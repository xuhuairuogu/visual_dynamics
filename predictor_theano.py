from collections import OrderedDict
import os
import numpy as np
import h5py
import cPickle
import theano
import theano.tensor as T
import lasagne
import lasagne.layers as L
from lasagne import init
from lasagne.utils import as_tuple
import predictor

def iterate_minibatches(*data, **kwargs):
    batch_size = kwargs.get('batch_size') or 1
    shuffle = kwargs.get('shuffle') or False
    N = len(data[0])
    for datum in data[1:]:
        assert len(datum) == N
    if shuffle:
        indices = np.arange(N)
        np.random.shuffle(indices)
    for start_idx in range(0, N - batch_size + 1, batch_size):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batch_size]
        else:
            excerpt = slice(start_idx, start_idx + batch_size)
        yield tuple(datum[excerpt] for datum in data)

def iterate_minibatches_indefinitely(hdf5_fname, *data_names, **kwargs):
    batch_size = kwargs.get('batch_size') or 1
    shuffle = kwargs.get('shuffle') or False
    with h5py.File(hdf5_fname, 'r+') as f:
        N = len(f[data_names[0]])
        for data_name in data_names[1:]:
            assert len(f[data_name]) == N
        indices = []
        while True:
            if len(indices) < batch_size:
                new_indices = np.arange(N)
                if shuffle:
                    np.random.shuffle(new_indices)
                indices.extend(new_indices)
            excerpt = indices[0:batch_size]
            if shuffle:
                sort_inds = np.argsort(excerpt)
                unsort_inds = [-1] * batch_size
                for unsort_ind, sort_ind in enumerate(sort_inds):
                    unsort_inds[sort_ind] = unsort_ind
                excerpt = np.asarray(excerpt)[sort_inds].tolist()
            else:
                excerpt = slice(0, batch_size)
            batch_data = tuple(f[data_name][excerpt] for data_name in data_names)
            if shuffle:
                for datum in batch_data:
                    datum[...] = datum[unsort_inds, ...]
            del indices[0:batch_size]
            yield batch_data

class Deconv2DLayer(L.Conv2DLayer):
    def __init__(self, incoming, channels, filter_size, stride=(1, 1),
                 pad=0, W=init.GlorotUniform(), b=init.Constant(0.),
                 nonlinearity=lasagne.nonlinearities.rectify, **kwargs):
        self.original_channels = channels
        self.original_filter = as_tuple(filter_size, 2)
        self.original_stride = as_tuple(stride, 2)

        if pad == 'valid':
            self.original_pad = (0, 0)
        elif pad in ('full', 'same'):
            self.original_pad = pad
        else:
            self.original_pad = as_tuple(pad, 2, int)

        super(Deconv2DLayer, self).__init__(incoming, channels, filter_size,
                                            stride=(1,1), pad='full', W=W, b=b,
                                            nonlinearity=nonlinearity, **kwargs)

    def get_output_shape_for(self, input_shape):
        _, _, width, height = input_shape
        original_width = ((width - 1) * self.original_stride[0]) - 2 * self.original_pad[0] + self.original_filter[0]
        original_height = ((height - 1) * self.original_stride[1]) - 2 * self.original_pad[1] + self.original_filter[1]
        return (input_shape[0], self.original_channels, original_width, original_height)

    def get_output_for(self, input, **kwargs):
        # first we upsample to compensate for strides
        if self.original_stride != 1:
            _, _, width, height = input.shape
            unstrided_width = width * self.original_stride[0]
            unstrided_height = height * self.original_stride[1]
            placeholder = T.zeros((input.shape[0], input.shape[1], unstrided_width, unstrided_height))
            upsampled = T.set_subtensor(placeholder[:, :, ::self.original_stride[0], ::self.original_stride[1]], input)
        else:
            upsampled = input
        # then we conv to deconv
        deconv = super(Deconv2DLayer, self).get_output_for(upsampled, input_shape=(None, self.input_shape[1], self.input_shape[2]*self.original_stride[0], self.input_shape[3]*self.original_stride[1]), **kwargs)
        # lastly we cut off original padding
        pad = self.original_pad
        _, _, original_width, original_height = self.get_output_shape_for(input.shape)
        t = deconv[:, :, pad[0]:(pad[0] + original_width), pad[1]:(pad[1] + original_height)]
        return t

class BilinearLayer(L.MergeLayer):
    def __init__(self, incomings, M=init.Normal(std=0.001),
                 N=init.Normal(std=0.001), b=init.Constant(0.), **kwargs):
        super(BilinearLayer, self).__init__(incomings, **kwargs)

        self.y_shape, self.u_shape = [input_shape[1:] for input_shape in self.input_shapes]
        self.y_dim = int(np.prod(self.y_shape))
        self.u_dim,  = self.u_shape

        self.M = self.add_param(M, (self.y_dim, self.y_dim, self.u_dim), name='M')
        self.N = self.add_param(N, (self.y_dim, self.u_dim), name='N')
        if b is None:
            self.b = None
        else:
            self.b = self.add_param(b, (self.y_dim,), name='b', regularizable=False)

    def get_output_shape_for(self, input_shapes):
        Y_shape, U_shape = input_shapes
        assert Y_shape[0] == U_shape[0]
        return (Y_shape[0], self.y_dim)

    def get_output_for(self, inputs, **kwargs):
        Y, U = inputs
        if Y.ndim > 2:
            Y = Y.flatten(2)

        outer_YU = Y[:, :, None] * U[:, None, :]
        activation = T.dot(outer_YU.flatten(2), self.M.reshape((self.y_dim, self.y_dim * self.u_dim)).T)
        activation = activation + T.dot(U, self.N.T)
        if self.b is not None:
            activation = activation + self.b.dimshuffle('x', 0)
        return activation

class TheanoNetFeaturePredictor(predictor.NetFeaturePredictor): # TODO: shouldn't be derived directly from NetFeaturePredictor
    def __init__(self, net_name, input_vars, pred_layers, loss, loss_deterministic=None, prediction_name=None, postfix=''):
        self.net_name = net_name
        self.postfix = postfix
        self.input_vars = input_vars
        self.pred_layers = pred_layers
        self.l_x_next_pred = pred_layers['X_next_pred']
        self.l_y_diff_pred = pred_layers['Y_diff_pred']
        self.loss = loss
        self.loss_deterministic = loss_deterministic or loss
        input_layers = {layer.input_var.name: layer for layer in lasagne.layers.get_all_layers(self.l_x_next_pred) if type(layer) == lasagne.layers.InputLayer}
        x_shape, u_shape = input_layers['X'].shape[1:], input_layers['U'].shape[1:]
        self.X_var, self.U_var, self.X_diff_var = input_vars.values()
        self.prediction_name = prediction_name or pred_layers.keys()[0]
        self.pred_vars = {}
        self.pred_fns = {}
        self.jacobian_var = self.jacobian_fn = None
        predictor.FeaturePredictor.__init__(self, x_shape, u_shape)

    def train(self, train_hdf5_fname, val_hdf5_fname=None,
              batch_size=32,
              test_iter = 10,
              solver_type = 'SGD',
              test_interval = 1000,
              base_lr = 0.05,
              gamma = 0.9,
              stepsize = 1000,
              display = 20,
              max_iter=10000,
              momentum = 0.9,
              momentum2 = 0.999,
              weight_decay=0.0005,
              snapshot=1000,
              snapshot_prefix=None):
        if snapshot_prefix is None:
            snapshot_prefix = self.get_snapshot_prefix()

        # training data
        minibatches = iterate_minibatches_indefinitely(train_hdf5_fname, 'image_curr', 'vel', 'image_diff',
                                                       batch_size=batch_size, shuffle=True)

        # training loss
        param_l2_penalty = lasagne.regularization.regularize_network_params(self.l_x_next_pred, lasagne.regularization.l2)
        loss = self.loss + weight_decay * param_l2_penalty / 2.

        # training function
        params = lasagne.layers.get_all_params(self.l_x_next_pred, trainable=True)
        learning_rate = theano.shared(base_lr)
        if solver_type == 'SGD':
            if momentum:
                updates = lasagne.updates.momentum(loss, params, learning_rate=learning_rate, momentum=momentum)
            else:
                updates = lasagne.updates.sgd(loss, params, learning_rate=learning_rate)
        elif solver_type == 'ADAM':
            updates = lasagne.updates.adam(loss, params, learning_rate=learning_rate, beta1=momentum, beta2=momentum2)
        else:
            raise
        train_fn = theano.function([self.X_var, self.U_var, self.X_diff_var], loss, updates=updates)

        validate = test_interval and val_hdf5_fname is not None
        if validate:
            # validation loss
            test_loss = self.loss_deterministic + weight_decay * param_l2_penalty / 2.

            # validation function
            val_fn = theano.function([self.X_var, self.U_var, self.X_diff_var], test_loss)
            val_fn = train_fn

        print("Starting training...")
        iter_ = 0
        while iter_ < max_iter:
            if validate and iter_ % test_interval == 0:
                self.test_all(val_fn, val_hdf5_fname, batch_size, test_iter)

            current_step = iter_ / stepsize
            rate = base_lr * gamma ** current_step
            learning_rate.set_value(rate)

            X, U, X_next = next(minibatches)
            loss = train_fn(X, U, X_next)

            if display and iter_ % display == 0:
                print("Iteration {} of {}, lr = {}".format(iter_, max_iter, learning_rate.get_value()))
                print("    training loss = {:.6f}".format(float(loss)))
            iter_ += 1
            if snapshot and iter_ % snapshot == 0 and iter_ > 0:
                self.snapshot(iter_, snapshot_prefix)

        if snapshot and not (snapshot and iter_ % snapshot == 0 and iter_ > 0):
            self.snapshot(iter_, snapshot_prefix)
        if display and iter_ % display == 0:
            print("Iteration {} of {}, lr = {}".format(iter_, max_iter, learning_rate.get_value()))
            print("    training loss = {:.6f}".format(float(loss)))
        if validate and iter_ % test_interval == 0:
            self.test_all(val_fn, val_hdf5_fname, batch_size, test_iter)

    def test_all(self, val_fn, val_hdf5_fname, batch_size, test_iter):
        loss = 0
        minibatches = iterate_minibatches_indefinitely(val_hdf5_fname, 'image_curr', 'vel', 'image_diff',
                                                       batch_size=batch_size, shuffle=False)
        for _ in range(test_iter):
            X, U, X_next = next(minibatches)
            loss += val_fn(X, U, X_next)
        print("    validation loss = {:.6f}".format(loss / test_iter))

    def snapshot(self, iter_, snapshot_prefix):
        snapshot_fname = snapshot_prefix + '_iter_%d.pkl'%iter_
        snapshot_file = file(snapshot_fname, 'wb')
        all_params = lasagne.layers.get_all_params(self.l_x_next_pred)
        print "Snapshotting to pickle file", snapshot_fname
        cPickle.dump(all_params, snapshot_file, protocol=cPickle.HIGHEST_PROTOCOL)
        snapshot_file.close()

    def predict(self, X, U, prediction_name=None):
        prediction_name = prediction_name or self.prediction_name
        if prediction_name == 'image_next_pred':
            prediction_name = 'X_next_pred'
        if prediction_name in self.pred_fns:
            pred_fn = self.pred_fns[prediction_name]
        else:
            if prediction_name not in self.pred_vars:
                self.pred_vars[prediction_name] = lasagne.layers.get_output(self.pred_layers[prediction_name], deterministic=True)
            input_vars = [self.X_var, self.U_var] if U is not None else [self.X_var]
            pred_fn = theano.function(input_vars, self.pred_vars[prediction_name])
            self.pred_fns[prediction_name] = pred_fn
        assert X.shape == self.x_shape or X.shape[1:] == self.x_shape
        batch = X.shape == self.x_shape
        if batch:
            X = X[None, ...]
            if U is not None:
                U = U[None, :]
        if U is None:
            pred = pred_fn(X)
        else:
            pred = pred_fn(X, U)
        if batch:
            pred = np.squeeze(pred, 0)
        return pred

    def jacobian_control(self, X, U):
        if self.jacobian_fn is None:
            prediction_name = 'Y_diff_pred'
            if prediction_name in self.pred_vars:
                Y_diff_pred_var = self.pred_vars[prediction_name]
            else:
                Y_diff_pred_var = lasagne.layers.get_output(self.pred_layers[prediction_name], deterministic=True)
                self.pred_vars[prediction_name] = Y_diff_pred_var
            self.jacobian_var = theano.gradient.jacobian(Y_diff_pred_var[0, :], self.U_var)
            self.jacobian_fn = theano.function([self.X_var, self.U_var], self.jacobian_var)
        if X.shape == self.x_shape:
            if U is None:
                U = np.zeros(self.u_shape)
            X, U = X[None, ...], U[None, :]
            jac = self.jacobian_fn(X, U)
            return np.squeeze(jac, 1)
        else:
            if U is None:
                U = np.zeros((len(X),) + self.u_shape)
            return np.asarray([self.jacobian_control(x, u) for x, u in zip(X, U)])

    def feature_from_input(self, X):
        return self.predict(X, None, 'Y')

    def get_model_dir(self):
        model_dir = os.path.join('theano_models', self.net_name + self.postfix)
        if not os.path.exists(model_dir):
            os.makedirs(model_dir)
        return model_dir

    def get_snapshot_prefix(self):
        snapshot_dir = os.path.join(self.get_model_dir(), 'snapshot')
        if not os.path.exists(snapshot_dir):
            os.makedirs(snapshot_dir)
        snapshot_prefix = os.path.join(snapshot_dir, '')
        return snapshot_prefix

def build_bilinear_net(input_shapes):
    x_shape, u_shape = input_shapes
    X_var = T.dtensor4('X')
    U_var = T.dmatrix('U')

    l_x = L.InputLayer(shape=(None,) + x_shape, input_var=X_var)
    l_u = L.InputLayer(shape=(None,) + u_shape, input_var=U_var)

    l_y_diff_pred = BilinearLayer([l_x, l_u], b=None)
    l_y = L.flatten(l_x)
    l_y_next_pred = L.ElemwiseMergeLayer([l_y, l_y_diff_pred], T.add)
    l_x_next_pred = L.ReshapeLayer(l_y_next_pred, ([0,],) + x_shape)

    X_next_pred_var = lasagne.layers.get_output(l_x_next_pred)
    X_diff_var = T.dtensor4('X_diff')
    X_next_var = X_var + X_diff_var
    loss = ((X_next_var - X_next_pred_var) ** 2).mean(axis=0).sum() / 2.

    net_name = 'BilinearNet'
    input_vars = OrderedDict([(var.name, var) for var in [X_var, U_var, X_diff_var]])
    pred_layers = OrderedDict([('Y_diff_pred', l_y_diff_pred), ('Y', l_y), ('X_next_pred', l_x_next_pred)])
    return net_name, input_vars, pred_layers, loss

def build_small_action_cond_encoder_net(input_shapes):
    x_shape, u_shape = input_shapes
    x2_c_dim = x1_c_dim = x_shape[0]
    x1_shape = (x1_c_dim, x_shape[1]//2, x_shape[2]//2)
    x2_shape = (x2_c_dim, x1_shape[1]//2, x1_shape[2]//2)
    y2_dim = 64
    X_var = T.dtensor4('X')
    U_var = T.dmatrix('U')

    l_x = L.InputLayer(shape=(None,) + x_shape, input_var=X_var)
    l_u = L.InputLayer(shape=(None,) + u_shape, input_var=U_var)

    l_x1 = L.Conv2DLayer(l_x, x1_c_dim, filter_size=6, stride=2, pad=2,
                         nonlinearity=lasagne.nonlinearities.rectify)
    l_x2 = L.Conv2DLayer(l_x1, x2_c_dim, filter_size=6, stride=2, pad=2,
                         nonlinearity=lasagne.nonlinearities.rectify)

    l_y2 = L.DenseLayer(l_x2, y2_dim, nonlinearity=None)
    l_y2_diff_pred = BilinearLayer([l_y2, l_u], b=None)
    l_y2_next_pred = L.ElemwiseMergeLayer([l_y2, l_y2_diff_pred], T.add)
    l_x2_next_pred_flat = L.DenseLayer(l_y2_next_pred, np.prod(x2_shape), nonlinearity=None)
    l_x2_next_pred = L.ReshapeLayer(l_x2_next_pred_flat, ([0],) + x2_shape)

    l_x1_next_pred = Deconv2DLayer(l_x2_next_pred, x2_c_dim, filter_size=6, stride=2, pad=2,
                                   nonlinearity=lasagne.nonlinearities.rectify)
    l_x_next_pred = Deconv2DLayer(l_x1_next_pred, x1_c_dim, filter_size=6, stride=2, pad=2,
                                  nonlinearity=lasagne.nonlinearities.tanh)

    l_y = l_y2
    l_y_diff_pred = l_y2_diff_pred

    X_next_pred_var = lasagne.layers.get_output(l_x_next_pred)
    X_diff_var = T.dtensor4('X_diff')
    X_next_var = X_var + X_diff_var
    loss = ((X_next_var - X_next_pred_var) ** 2).mean(axis=0).sum() / 2.

    net_name = 'SmallActionCondEncoderNet'
    input_vars = OrderedDict([(var.name, var) for var in [X_var, U_var, X_diff_var]])
    pred_layers = OrderedDict([('Y_diff_pred', l_y_diff_pred), ('Y', l_y), ('X_next_pred', l_x_next_pred)])
    return net_name, input_vars, pred_layers, loss