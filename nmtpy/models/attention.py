from six.moves import range
from six.moves import zip

# Python
import os
import cPickle
import inspect
import importlib

from collections import OrderedDict

# 3rd party
import numpy as np

import theano
import theano.tensor as tensor

# Ours
from nmtpy.layers import *
from nmtpy.typedef import *
from nmtpy.nmtutils import *
from nmtpy.search import beam_search
from nmtpy.iterators import get_iterator
from nmtpy.models.basemodel import BaseModel
from nmtpy.sysutils import get_valid_evaluation

class Model(BaseModel):
    def __init__(self, trng, **kwargs):
        # Call parent's init first
        super(Model, self).__init__(**kwargs)

        # Load vocabularies if any
        if 'dicts' in kwargs:
            dicts = kwargs['dicts']
            if 'src' in dicts:
                self.src_dict, src_idict = load_dictionary(dicts['src'])
                self.n_words_src = min(self.n_words_src, len(self.src_dict)) if self.n_words_src > 0 else len(self.src_dict)
            if 'trg' in dicts:
                self.trg_dict, trg_idict = load_dictionary(dicts['trg'])
                self.n_words_trg = min(self.n_words_trg, len(self.trg_dict)) if self.n_words_trg > 0 else len(self.trg_dict)

        self.options = dict(self.__dict__)
        self.trg_idict = trg_idict
        self.src_idict = src_idict

        self.ctx_dim = 2 * self.rnn_dim
        self.set_nanguard()
        self.set_trng(trng)

    def load_data(self, shuffle=False, sort=False):
        # We need to find out about modalities
        train_src_type, train_src_file = self.data['train_src']
        train_trg_type, train_trg_file = self.data['train_trg']
        train_src_iter_class = get_iterator(train_src_type)
        train_trg_iter_class = get_iterator(train_trg_type)

        # This is the only option
        assert train_src_type == train_trg_type == 'bitext'

        self.train_iterator = train_src_iter_class(train_src_file, self.src_dict,
                                                   train_trg_file, self.trg_dict,
                                                   batch_size=self.batch_size,
                                                   n_words_src=self.n_words_src, n_words_trg=self.n_words_trg)

        # Prepare batches
        self.train_iterator.prepare_batches(shuffle=shuffle, sort=sort)

        if 'valid_src' in self.data:
            # Validation data available
            valid_src_type, valid_src_file = self.data['valid_src']
            valid_trg_type, self.valid_trg_file = self.data['valid_trg']
            valid_src_iter_class = get_iterator(valid_src_type)
            valid_trg_iter_class = get_iterator(valid_trg_type)
            assert valid_src_type == valid_trg_type == 'bitext'

            self.valid_iterator = valid_src_iter_class(valid_src_file, self.src_dict,
                                                       self.valid_trg_file, self.trg_dict, batch_size=64,
                                                       n_words_src=self.n_words_src, n_words_trg=self.n_words_trg)
            self.valid_iterator.prepare_batches()

    def init_params(self):
        params = OrderedDict()

        # embedding weights for encoder and decoder
        params['Wemb_enc'] = norm_weight(self.n_words_src, self.embedding_dim)
        params['Wemb_dec'] = norm_weight(self.n_words_trg, self.embedding_dim)

        # encoder: bidirectional RNN
        #########
        # Forward encoder
        params = get_new_layer(self.enc_type)[0](params, prefix='encoder', nin=self.embedding_dim, dim=self.rnn_dim)
        # Backwards encoder
        params = get_new_layer(self.enc_type)[0](params, prefix='encoder_r', nin=self.embedding_dim, dim=self.rnn_dim)

        # Context is the concatenation of forward and backwards encoder

        # init_state, init_cell
        params = get_new_layer('ff')[0](params, prefix='ff_state', nin=self.ctx_dim, nout=self.rnn_dim)
        # decoder
        params = get_new_layer(self.dec_type)[0](params, prefix='decoder', nin=self.embedding_dim, dim=self.rnn_dim, dimctx=self.ctx_dim)

        # readout
        params = get_new_layer('ff')[0](params, prefix='ff_logit_gru'   , nin=self.rnn_dim, nout=self.embedding_dim, ortho=False)
        params = get_new_layer('ff')[0](params, prefix='ff_logit_prev'  , nin=self.embedding_dim, nout=self.embedding_dim, ortho=False)
        params = get_new_layer('ff')[0](params, prefix='ff_logit_ctx'   , nin=self.ctx_dim, nout=self.embedding_dim, ortho=False)
        params = get_new_layer('ff')[0](params, prefix='ff_logit'       , nin=self.embedding_dim, nout=self.n_words_trg)

        self.initial_params = params

    def build(self):
        # description string: #words x #samples
        x = tensor.matrix('x', dtype=INT)
        x_mask = tensor.matrix('x_mask', dtype='float32')
        y = tensor.matrix('y', dtype=INT)
        y_mask = tensor.matrix('y_mask', dtype='float32')

        self.inputs['x'] = x
        self.inputs['x_mask'] = x_mask
        self.inputs['y'] = y
        self.inputs['y_mask'] = y_mask

        # for the backward rnn, we just need to invert x and x_mask
        xr = x[::-1]
        xr_mask = x_mask[::-1]

        n_timesteps = x.shape[0]
        n_timesteps_trg = y.shape[0]
        n_samples = x.shape[1]

        # word embedding for forward rnn (source)
        emb = self.tparams['Wemb_enc'][x.flatten()]
        emb = emb.reshape([n_timesteps, n_samples, self.embedding_dim])
        proj = get_new_layer(self.enc_type)[1](self.tparams, emb, prefix='encoder', mask=x_mask,
                                               profile=self.profile, mode=self.func_mode)

        # word embedding for backward rnn (source)
        embr = self.tparams['Wemb_enc'][xr.flatten()]
        embr = embr.reshape([n_timesteps, n_samples, self.embedding_dim])
        projr = get_new_layer(self.enc_type)[1](self.tparams, embr, prefix='encoder_r', mask=xr_mask,
                                                profile=self.profile, mode=self.func_mode)

        # context will be the concatenation of forward and backward rnns
        ctx = tensor.concatenate([proj[0], projr[0][::-1]], axis=proj[0].ndim-1)

        # mean of the context (across time) will be used to initialize decoder rnn
        ctx_mean = (ctx * x_mask[:, :, None]).sum(0) / x_mask.sum(0)[:, None]

        # NOTE: Tried this, no improvement
        # or you can use the last state of forward + backward encoder rnns
        # ctx_mean = tensor.concatenate([proj[0][-1], projr[0][-1]], axis=proj[0].ndim-2)

        # initial decoder state
        init_state = get_new_layer('ff')[1](self.tparams, ctx_mean, prefix='ff_state', activ='tanh')

        # word embedding (target), we will shift the target sequence one time step
        # to the right. This is done because of the bi-gram connections in the
        # readout and decoder rnn. The first target will be all zeros and we will
        # not condition on the last output.
        emb = self.tparams['Wemb_dec'][y.flatten()]
        emb = emb.reshape([n_timesteps_trg, n_samples, self.embedding_dim])
        emb_shifted = tensor.zeros_like(emb)
        emb_shifted = tensor.set_subtensor(emb_shifted[1:], emb[:-1])
        emb = emb_shifted

        # decoder - pass through the decoder conditional gru with attention
        proj = get_new_layer(self.dec_type)[1](self.tparams, emb,
                                                    prefix='decoder',
                                                    mask=y_mask, context=ctx,
                                                    context_mask=x_mask,
                                                    one_step=False,
                                                    init_state=init_state,
                                                    profile=self.profile,
                                                    mode=self.func_mode)
        # hidden states of the decoder gru
        proj_h = proj[0]

        # weighted averages of context, generated by attention module
        ctxs = proj[1]

        # weights (alignment matrix)
        alphas = proj[2]

        # compute word probabilities
        logit_gru = get_new_layer('ff')[1](self.tparams, proj_h, prefix='ff_logit_gru', activ='linear')
        logit_prev = get_new_layer('ff')[1](self.tparams, emb, prefix='ff_logit_prev', activ='linear')
        logit_ctx = get_new_layer('ff')[1](self.tparams, ctxs, prefix='ff_logit_ctx', activ='linear')

        logit = tanh(logit_gru + logit_prev + logit_ctx)

        if self.dropout > 0:
            logit = dropout_layer(logit, self.use_dropout, self.dropout, self.trng)

        logit = get_new_layer('ff')[1](self.tparams, logit, prefix='ff_logit', activ='linear')
        logit_shp = logit.shape

        # Apply logsoftmax (stable version)
        log_probs = -tensor.nnet.logsoftmax(logit.reshape([logit_shp[0]*logit_shp[1], logit_shp[2]]))

        # cost
        y_flat = y.flatten()
        y_flat_idx = tensor.arange(y_flat.shape[0]) * self.n_words_trg + y_flat

        cost = log_probs.flatten()[y_flat_idx]
        cost = cost.reshape([y.shape[0], y.shape[1]])
        cost = (cost * y_mask).sum(0)

        # Useful for debugging from ipython
        #self.db = locals()

        # NOTE: We may want to normalize the cost by dividing
        # to the number of target tokens but this needs
        # scaling the learning rate accordingly.
        # cost = cost / y_mask.sum()

        self.f_log_probs = theano.function(self.inputs.values(),
                                           cost,
                                           mode=self.func_mode,
                                           profile=self.profile)

        self.cost = cost.mean()

        return self.cost

    def build_sampler(self):
        x = tensor.matrix('x', dtype=INT)
        xr = x[::-1]
        n_timesteps = x.shape[0]
        n_samples = x.shape[1]

        # word embedding (source), forward and backward
        emb = self.tparams['Wemb_enc'][x.flatten()]
        emb = emb.reshape([n_timesteps, n_samples, self.embedding_dim])

        embr = self.tparams['Wemb_enc'][xr.flatten()]
        embr = embr.reshape([n_timesteps, n_samples, self.embedding_dim])

        # encoder
        proj = get_new_layer(self.enc_type)[1](self.tparams, emb, prefix='encoder')
        projr = get_new_layer(self.enc_type)[1](self.tparams, embr, prefix='encoder_r')

        # concatenate forward and backward rnn hidden states
        ctx = tensor.concatenate([proj[0], projr[0][::-1]], axis=proj[0].ndim-1)

        # get the input for decoder rnn initializer mlp
        ctx_mean = ctx.mean(0)
        # ctx_mean = tensor.concatenate([proj[0][-1],projr[0][-1]], axis=proj[0].ndim-2)
        init_state = get_new_layer('ff')[1](self.tparams, ctx_mean, prefix='ff_state', activ='tanh')

        outs = [init_state, ctx]
        self.f_init = theano.function([x], outs, name='f_init', profile=self.profile)

        # x: 1 x 1
        y = tensor.vector('y_sampler', dtype=INT)
        init_state = tensor.matrix('init_state', dtype='float32')

        # if it's the first word, emb should be all zero and it is indicated by -1
        emb = tensor.switch(y[:, None] < 0,
                            tensor.alloc(0., 1, self.tparams['Wemb_dec'].shape[1]),
                            self.tparams['Wemb_dec'][y])

        # apply one step of conditional gru with attention
        # get the next hidden state
        # get the weighted averages of context for this target word y
        r = get_new_layer(self.dec_type)[1](self.tparams, emb,
                                                    prefix='decoder',
                                                    mask=None, context=ctx,
                                                    one_step=True,
                                                    init_state=init_state)

        next_state = r[0]
        ctxs = r[1]

        logit_prev = get_new_layer('ff')[1](self.tparams, emb,          prefix='ff_logit_prev',activ='linear')
        logit_ctx  = get_new_layer('ff')[1](self.tparams, ctxs,         prefix='ff_logit_ctx', activ='linear')
        logit_gru  = get_new_layer('ff')[1](self.tparams, next_state,   prefix='ff_logit_gru', activ='linear')

        logit = tanh(logit_gru + logit_prev + logit_ctx)

        if self.dropout > 0:
            logit = dropout_layer(logit, self.use_dropout, self.dropout, self.trng)

        logit = get_new_layer('ff')[1](self.tparams, logit, prefix='ff_logit', activ='linear')

        # compute the logsoftmax
        next_log_probs = tensor.nnet.logsoftmax(logit)

        # compile a function to do the whole thing above
        # next hidden state to be used
        inputs = [y, ctx, init_state]
        outs = [next_log_probs, next_state]
        self.f_next = theano.function(inputs, outs, name='f_next', profile=self.profile)

    # Not used for now
    def beam_search(self, beam_size=12):
        tmp_model = os.path.join("/tmp", self.name) + ".npz"
        tmp_opts = "%s.pkl" % tmp_model
        # Save model temporarily
        self.save_params(tmp_model, **unzip(self.tparams))
        self.save_options(filepath=tmp_opts)
        result = get_valid_evaluation(tmp_model, beam_size)
        os.unlink(tmp_model)
        os.unlink(tmp_opts)
        return result
        #hyps = []
        #for data in self.valid_iterator:
            #xs = data['x'].T.astype(np.int64)
            ## Consume validation data sample by sample for beam search
            #for x in xs:
                #sample, score = beam_search(self.f_init, self.f_next, [x[:, None]],
                                            #beam_size=beam_size, maxlen=self.maxlen)
                ## Normalize by lengths and find the best hypothesis
                #lens = np.array([len(s) for s in sample])
                #score = np.array(score) / lens
                #hyps.append(idx_to_sent(self.trg_idict, sample[np.argmin(score)]))

        #return self.valid_scorer.compute(self.valid_trg_file, hyps)
