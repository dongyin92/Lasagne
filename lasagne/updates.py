"""
Functions to generate Theano update dictionaries for training.

The update functions implement different methods to control the learning
rate for use with stochastic gradient descent.

Update functions take a loss expression or a list of gradient expressions and
a list of parameters as input and return an ordered dictionary of updates:

.. autosummary::
    :nosignatures:

    sgd
    momentum
    nesterov_momentum
    adagrad
    rmsprop
    adadelta
    adam
    adamax

Two functions can be used to further modify the updates to include momentum:

.. autosummary::
    :nosignatures:

    apply_momentum
    apply_nesterov_momentum

Finally, we provide two helper functions to constrain the norm of tensors:

.. autosummary::
    :nosignatures:

    norm_constraint
    total_norm_constraint

:func:`norm_constraint()` can be used to constrain the norm of parameters
(as an alternative to weight decay), or for a form of gradient clipping.
:func:`total_norm_constraint()` constrain the total norm of a list of tensors.
This is often used when training recurrent neural networks.

Examples
--------
>>> import lasagne
>>> import theano.tensor as T
>>> import theano
>>> from lasagne.nonlinearities import softmax
>>> from lasagne.layers import InputLayer, DenseLayer, get_output
>>> from lasagne.updates import sgd, apply_momentum
>>> l_in = InputLayer((100, 20))
>>> l1 = DenseLayer(l_in, num_units=3, nonlinearity=softmax)
>>> x = T.matrix('x')  # shp: num_batch x num_features
>>> y = T.ivector('y') # shp: num_batch
>>> l_out = get_output(l1, x)
>>> params = lasagne.layers.get_all_params(l1)
>>> loss = T.mean(T.nnet.categorical_crossentropy(l_out, y))
>>> updates_sgd = sgd(loss, params, learning_rate=0.0001)
>>> updates = apply_momentum(updates_sgd, params, momentum=0.9)
>>> train_function = theano.function([x, y], updates=updates)
"""

from collections import OrderedDict
from functools import wraps

import numpy as np

import theano
import theano.tensor as T
from theano.ifelse import ifelse
from . import utils

__all__ = [
    "sgd",
    "apply_momentum",
    "momentum",
    "apply_nesterov_momentum",
    "nesterov_momentum",
    "adagrad",
    "rmsprop",
    "adadelta",
    "adam",
    "adamax",
    "cocob",
    "yellow_fin",
    "norm_constraint",
    "total_norm_constraint",
    "apply_decay",
    "lr_decay",
    "exponential_moving_average",
    "ema"
]


def apply_decay(updates, params, period=0, factor=0.5):
    if period <= 0:
        return updates

    for p in params:
        count = theano.shared(1, p.name+utils.SCOPE_DELIMITER+"decay_count")
        one = T.constant(1, dtype=count.dtype)
        cond = T.ge(count, period)
        updates[count] = ifelse(cond, one, count+one)
        updates[p] = ifelse(cond, p*factor, p)
    return updates


def lr_decay(updates_fn):
    @wraps(updates_fn)
    def get_updates(loss_or_grads, params, learning_rate, *args,
                    decay_period=0, decay_factor=0.5, **kwargs):
        if decay_period <= 0 or decay_factor == 1:
            return updates_fn(
                loss_or_grads, params, learning_rate, *args, **kwargs)

        learning_rate = theano.shared(
            utils.floatX(learning_rate), "learning_rate")
        updates = updates_fn(loss_or_grads, params, learning_rate,
                             *args, **kwargs)
        updates = apply_decay(updates, [learning_rate],
                              decay_period, decay_factor)
        return updates
    return get_updates


def get_or_compute_grads(loss_or_grads, params):
    """Helper function returning a list of gradients

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to return the gradients for

    Returns
    -------
    list of expressions
        If `loss_or_grads` is a list, it is assumed to be a list of
        gradients and returned as is, unless it does not match the length
        of `params`, in which case a `ValueError` is raised.
        Otherwise, `loss_or_grads` is assumed to be a cost expression and
        the function returns `theano.grad(loss_or_grads, params)`.

    Raises
    ------
    ValueError
        If `loss_or_grads` is a list of a different length than `params`, or if
        any element of `params` is not a shared variable (while we could still
        compute its gradient, we can never update it and want to fail early).
    """
    if any(not isinstance(p, theano.compile.SharedVariable) for p in params):
        raise ValueError("params must contain shared variables only. If it "
                         "contains arbitrary parameter expressions, then "
                         "lasagne.utils.collect_shared_vars() may help you.")
    if isinstance(loss_or_grads, list):
        if not len(loss_or_grads) == len(params):
            raise ValueError("Got %d gradient expressions for %d parameters" %
                             (len(loss_or_grads), len(params)))
        return loss_or_grads
    else:
        return theano.grad(loss_or_grads, params)


def exponential_moving_average(alpha, s_t, x_t, t=None, init_period=None):
    """
    Computes the exponential moving average of the process s_t:

    s_{t+1} = alpha_t * s_t + (1 - alpha_t) * x_t

    If t is None or init_period is None than it is assumed alpha_t is constant.
    Otherwise, assuming T = init_period:

    alpha_t = alpha * min(1, (T - 1) * t / T^2 + 1 / T)

    Parameters
    ----------
    alpha : int, float, np.ndarray or Theano expression
        Smoothing coefficient
    s_t : int, float, np.ndarray or Theano expression
        State of the system at time t.
    x_t : int, float, np.ndarray or Theano expression
        Observation at time t.
    t : int, Theano expression or None
        Current time.
    init_period : int, Theano expression or None
        The initial period with reduced smoothing.

    Returns
    -------
    The state of the system at time t + 1.
    """
    alpha = utils.th_fx(alpha)
    factor = T.constant(1)
    if t is not None and init_period is not None and init_period > 0:
        p = utils.th_fx(init_period)
        t = utils.th_fx(t)
        alpha *= T.minimum(1.0, (p - 1.0) * t / T.sqr(p) + T.inv(p))
    elif t is not None:
        factor = 1 - alpha**utils.th_fx(t + 1)
    return (alpha * s_t + (1 - alpha) * x_t) / factor

ema = exponential_moving_average


@lr_decay
def sgd(loss_or_grads, params, learning_rate):
    """Stochastic Gradient Descent (SGD) updates

    Generates update expressions of the form:

    * ``param := param - learning_rate * gradient``

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float or symbolic scalar
        The learning rate controlling the size of update steps

    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression
    """
    grads = get_or_compute_grads(loss_or_grads, params)
    updates = OrderedDict()

    for param, grad in zip(params, grads):
        updates[param] = param - learning_rate * grad

    return updates


def apply_momentum(updates, params=None, momentum=0.9, velocities=None):
    """Returns a modified update dictionary including momentum

    Generates update expressions of the form:

    * ``velocity := momentum * velocity + updates[param] - param``
    * ``param := param + velocity``

    Parameters
    ----------
    updates : OrderedDict
        A dictionary mapping parameters to update expressions
    params : iterable of shared variables, optional
        The variables to apply momentum to. If omitted, will apply
        momentum to all `updates.keys()`.
    momentum : float or symbolic scalar, optional
        The amount of momentum to apply. Higher momentum results in
        smoothing over more update steps. Defaults to 0.9.
    velocities: list of shared variables
        Initial already created velocities variables
    
    Returns
    -------
    OrderedDict
        A copy of `updates` with momentum updates for all `params`.

    Notes
    -----
    Higher momentum also results in larger update steps. To counter that,
    you can optionally scale your learning rate by `1 - momentum`.

    See Also
    --------
    momentum : Shortcut applying momentum to SGD updates
    """
    if params is None:
        params = updates.keys()
    updates = OrderedDict(updates)

    def make_velocity(param):
        value = param.get_value(borrow=True)
        return theano.shared(np.zeros(value.shape, dtype=value.dtype),
                             broadcastable=param.broadcastable)
    velocities = [make_velocity(p) for p in params] if velocities is None else velocities
    for param, velocity in zip(params, velocities):
        x = momentum * velocity + updates[param]
        updates[velocity] = x - param
        updates[param] = x

    return updates


@lr_decay
def momentum(loss_or_grads, params, learning_rate, momentum=0.9, velocities=None):
    """Stochastic Gradient Descent (SGD) updates with momentum

    Generates update expressions of the form:

    * ``velocity := momentum * velocity - learning_rate * gradient``
    * ``param := param + velocity``

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float or symbolic scalar
        The learning rate controlling the size of update steps
    momentum : float or symbolic scalar, optional
        The amount of momentum to apply. Higher momentum results in
        smoothing over more update steps. Defaults to 0.9.
    velocities: list of shared variables
        Initial already created velocities variables
        
    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression

    Notes
    -----
    Higher momentum also results in larger update steps. To counter that,
    you can optionally scale your learning rate by `1 - momentum`.

    See Also
    --------
    apply_momentum : Generic function applying momentum to updates
    nesterov_momentum : Nesterov's variant of SGD with momentum
    """
    updates = sgd(loss_or_grads, params, learning_rate)
    return apply_momentum(updates, momentum=momentum, velocities=velocities)


def apply_nesterov_momentum(updates, params=None, momentum=0.9, velocities=None):
    """Returns a modified update dictionary including Nesterov momentum

    Generates update expressions of the form:

    * ``velocity := momentum * velocity + updates[param] - param``
    * ``param := param + momentum * velocity + updates[param] - param``

    Parameters
    ----------
    updates : OrderedDict
        A dictionary mapping parameters to update expressions
    params : iterable of shared variables, optional
        The variables to apply momentum to. If omitted, will apply
        momentum to all `updates.keys()`.
    momentum : float or symbolic scalar, optional
        The amount of momentum to apply. Higher momentum results in
        smoothing over more update steps. Defaults to 0.9.
    velocities: list of shared variables
        Initial already created velocities variables
        
    Returns
    -------
    OrderedDict
        A copy of `updates` with momentum updates for all `params`.

    Notes
    -----
    Higher momentum also results in larger update steps. To counter that,
    you can optionally scale your learning rate by `1 - momentum`.

    The classic formulation of Nesterov momentum (or Nesterov accelerated
    gradient) requires the gradient to be evaluated at the predicted next
    position in parameter space. Here, we use the formulation described at
    https://github.com/lisa-lab/pylearn2/pull/136#issuecomment-10381617,
    which allows the gradient to be evaluated at the current parameters.

    See Also
    --------
    nesterov_momentum : Shortcut applying Nesterov momentum to SGD updates
    """
    if params is None:
        params = updates.keys()
    updates = OrderedDict(updates)

    def make_velocity(param):
        value = param.get_value(borrow=True)
        return theano.shared(np.zeros(value.shape, dtype=value.dtype),
                             broadcastable=param.broadcastable)
    velocities = [make_velocity(p) for p in params] if velocities is None else velocities
    for param, velocity in zip(params, velocities):
        x = momentum * velocity + updates[param] - param
        updates[velocity] = x
        updates[param] = momentum * x + updates[param]

    return updates


@lr_decay
def nesterov_momentum(loss_or_grads, params, learning_rate, momentum=0.9, velocities=None):
    """Stochastic Gradient Descent (SGD) updates with Nesterov momentum

    Generates update expressions of the form:

    * ``velocity := momentum * velocity - learning_rate * gradient``
    * ``param := param + momentum * velocity - learning_rate * gradient``

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float or symbolic scalar
        The learning rate controlling the size of update steps
    momentum : float or symbolic scalar, optional
        The amount of momentum to apply. Higher momentum results in
        smoothing over more update steps. Defaults to 0.9.
    velocities: list of shared variables
        Initial already created velocities variables

    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression

    Notes
    -----
    Higher momentum also results in larger update steps. To counter that,
    you can optionally scale your learning rate by `1 - momentum`.

    The classic formulation of Nesterov momentum (or Nesterov accelerated
    gradient) requires the gradient to be evaluated at the predicted next
    position in parameter space. Here, we use the formulation described at
    https://github.com/lisa-lab/pylearn2/pull/136#issuecomment-10381617,
    which allows the gradient to be evaluated at the current parameters.

    See Also
    --------
    apply_nesterov_momentum : Function applying momentum to updates
    """
    updates = sgd(loss_or_grads, params, learning_rate)
    return apply_nesterov_momentum(updates, momentum=momentum, velocities=velocities)


@lr_decay
def adagrad(loss_or_grads, params, learning_rate=1.0, epsilon=1e-6):
    """Adagrad updates

    Scale learning rates by dividing with the square root of accumulated
    squared gradients. See [1]_ for further description.

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float or symbolic scalar
        The learning rate controlling the size of update steps
    epsilon : float or symbolic scalar
        Small value added for numerical stability

    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression

    Notes
    -----
    Using step size eta Adagrad calculates the learning rate for feature i at
    time step t as:

    .. math:: \\eta_{t,i} = \\frac{\\eta}
       {\\sqrt{\\sum^t_{t^\\prime} g^2_{t^\\prime,i}+\\epsilon}} g_{t,i}

    as such the learning rate is monotonically decreasing.

    Epsilon is not included in the typical formula, see [2]_.

    References
    ----------
    .. [1] Duchi, J., Hazan, E., & Singer, Y. (2011):
           Adaptive subgradient methods for online learning and stochastic
           optimization. JMLR, 12:2121-2159.

    .. [2] Chris Dyer:
           Notes on AdaGrad. http://www.ark.cs.cmu.edu/cdyer/adagrad.pdf
    """

    grads = get_or_compute_grads(loss_or_grads, params)
    updates = OrderedDict()

    for param, grad in zip(params, grads):
        value = param.get_value(borrow=True)
        accu = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                             broadcastable=param.broadcastable)
        accu_new = accu + grad ** 2
        updates[accu] = accu_new
        updates[param] = param - (learning_rate * grad /
                                  T.sqrt(accu_new + epsilon))

    return updates


@lr_decay
def rmsprop(loss_or_grads, params, learning_rate=1.0, rho=0.9, epsilon=1e-6):
    """RMSProp updates

    Scale learning rates by dividing with the moving average of the root mean
    squared (RMS) gradients. See [1]_ for further description.

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float or symbolic scalar
        The learning rate controlling the size of update steps
    rho : float or symbolic scalar
        Gradient moving average decay factor
    epsilon : float or symbolic scalar
        Small value added for numerical stability

    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression

    Notes
    -----
    `rho` should be between 0 and 1. A value of `rho` close to 1 will decay the
    moving average slowly and a value close to 0 will decay the moving average
    fast.

    Using the step size :math:`\\eta` and a decay factor :math:`\\rho` the
    learning rate :math:`\\eta_t` is calculated as:

    .. math::
       r_t &= \\rho r_{t-1} + (1-\\rho)*g^2\\\\
       \\eta_t &= \\frac{\\eta}{\\sqrt{r_t + \\epsilon}}

    References
    ----------
    .. [1] Tieleman, T. and Hinton, G. (2012):
           Neural Networks for Machine Learning, Lecture 6.5 - rmsprop.
           Coursera. http://www.youtube.com/watch?v=O3sxAc4hxZU (formula @5:20)
    """
    grads = get_or_compute_grads(loss_or_grads, params)
    updates = OrderedDict()

    # Using theano constant to prevent upcasting of float32
    one = T.constant(1)

    for param, grad in zip(params, grads):
        value = param.get_value(borrow=True)
        accu = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                             broadcastable=param.broadcastable)
        accu_new = rho * accu + (one - rho) * grad ** 2
        updates[accu] = accu_new
        updates[param] = param - (learning_rate * grad /
                                  T.sqrt(accu_new + epsilon))

    return updates


@lr_decay
def adadelta(loss_or_grads, params, learning_rate=1.0, rho=0.95, epsilon=1e-6):
    """ Adadelta updates

    Scale learning rates by the ratio of accumulated gradients to accumulated
    updates, see [1]_ and notes for further description.

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float or symbolic scalar
        The learning rate controlling the size of update steps
    rho : float or symbolic scalar
        Squared gradient moving average decay factor
    epsilon : float or symbolic scalar
        Small value added for numerical stability

    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression

    Notes
    -----
    rho should be between 0 and 1. A value of rho close to 1 will decay the
    moving average slowly and a value close to 0 will decay the moving average
    fast.

    rho = 0.95 and epsilon=1e-6 are suggested in the paper and reported to
    work for multiple datasets (MNIST, speech).

    In the paper, no learning rate is considered (so learning_rate=1.0).
    Probably best to keep it at this value.
    epsilon is important for the very first update (so the numerator does
    not become 0).

    Using the step size eta and a decay factor rho the learning rate is
    calculated as:

    .. math::
       r_t &= \\rho r_{t-1} + (1-\\rho)*g^2\\\\
       \\eta_t &= \\eta \\frac{\\sqrt{s_{t-1} + \\epsilon}}
                             {\sqrt{r_t + \epsilon}}\\\\
       s_t &= \\rho s_{t-1} + (1-\\rho)*(\\eta_t*g)^2

    References
    ----------
    .. [1] Zeiler, M. D. (2012):
           ADADELTA: An Adaptive Learning Rate Method.
           arXiv Preprint arXiv:1212.5701.
    """
    grads = get_or_compute_grads(loss_or_grads, params)
    updates = OrderedDict()

    # Using theano constant to prevent upcasting of float32
    one = T.constant(1)

    for param, grad in zip(params, grads):
        value = param.get_value(borrow=True)
        # accu: accumulate gradient magnitudes
        accu = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                             broadcastable=param.broadcastable)
        # delta_accu: accumulate update magnitudes (recursively!)
        delta_accu = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                                   broadcastable=param.broadcastable)

        # update accu (as in rmsprop)
        accu_new = rho * accu + (one - rho) * grad ** 2
        updates[accu] = accu_new

        # compute parameter update, using the 'old' delta_accu
        update = (grad * T.sqrt(delta_accu + epsilon) /
                  T.sqrt(accu_new + epsilon))
        updates[param] = param - learning_rate * update

        # update delta_accu (as accu, but accumulating updates)
        delta_accu_new = rho * delta_accu + (one - rho) * update ** 2
        updates[delta_accu] = delta_accu_new

    return updates


@lr_decay
def adam(loss_or_grads, params, learning_rate=0.001, beta1=0.9,
         beta2=0.999, epsilon=1e-8):
    """Adam updates

    Adam updates implemented as in [1]_.

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float
        Learning rate
    beta1 : float
        Exponential decay rate for the first moment estimates.
    beta2 : float
        Exponential decay rate for the second moment estimates.
    epsilon : float
        Constant for numerical stability.

    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression

    Notes
    -----
    The paper [1]_ includes an additional hyperparameter lambda. This is only
    needed to prove convergence of the algorithm and has no practical use
    (personal communication with the authors), it is therefore omitted here.

    References
    ----------
    .. [1] Kingma, Diederik, and Jimmy Ba (2014):
           Adam: A Method for Stochastic Optimization.
           arXiv preprint arXiv:1412.6980.
    """
    all_grads = get_or_compute_grads(loss_or_grads, params)
    t_prev = theano.shared(utils.floatX(0.), name="Adam_t")
    updates = OrderedDict()

    # Using theano constant to prevent upcasting of float32
    one = T.constant(1)

    t = t_prev + 1
    a_t = learning_rate*T.sqrt(one-beta2**t)/(one-beta1**t)

    for param, g_t in zip(params, all_grads):
        value = param.get_value(borrow=True)
        m_prev = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                               broadcastable=param.broadcastable,
                               name="Adam_mu::" + param.name)
        v_prev = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                               broadcastable=param.broadcastable,
                               name="Adam_var::" + param.name)

        m_t = beta1*m_prev + (one-beta1)*g_t
        v_t = beta2*v_prev + (one-beta2)*g_t**2
        step = a_t*m_t/(T.sqrt(v_t) + epsilon)

        updates[m_prev] = m_t
        updates[v_prev] = v_t
        updates[param] = param - step

    updates[t_prev] = t
    return updates


@lr_decay
def adamax(loss_or_grads, params, learning_rate=0.002, beta1=0.9,
           beta2=0.999, epsilon=1e-8):
    """Adamax updates

    Adamax updates implemented as in [1]_. This is a variant of of the Adam
    algorithm based on the infinity norm.

    Parameters
    ----------
    loss_or_grads : symbolic expression or list of expressions
        A scalar loss expression, or a list of gradient expressions
    params : list of shared variables
        The variables to generate update expressions for
    learning_rate : float
        Learning rate
    beta1 : float
        Exponential decay rate for the first moment estimates.
    beta2 : float
        Exponential decay rate for the weighted infinity norm estimates.
    epsilon : float
        Constant for numerical stability.

    Returns
    -------
    OrderedDict
        A dictionary mapping each parameter to its update expression

    References
    ----------
    .. [1] Kingma, Diederik, and Jimmy Ba (2014):
           Adam: A Method for Stochastic Optimization.
           arXiv preprint arXiv:1412.6980.
    """
    all_grads = get_or_compute_grads(loss_or_grads, params)
    t_prev = theano.shared(utils.floatX(0.))
    updates = OrderedDict()

    # Using theano constant to prevent upcasting of float32
    one = T.constant(1)

    t = t_prev + 1
    a_t = learning_rate/(one-beta1**t)

    for param, g_t in zip(params, all_grads):
        value = param.get_value(borrow=True)
        m_prev = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                               broadcastable=param.broadcastable)
        u_prev = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                               broadcastable=param.broadcastable)

        m_t = beta1*m_prev + (one-beta1)*g_t
        u_t = T.maximum(beta2*u_prev, abs(g_t))
        step = a_t*m_t/(u_t + epsilon)

        updates[m_prev] = m_t
        updates[u_prev] = u_t
        updates[param] = param - step

    updates[t_prev] = t
    return updates


def cocob(loss_or_grads, params, alpha=1000, epsilon=1e-9, use_sigmoid=False):
    grads = get_or_compute_grads(loss_or_grads, params)
    updates = OrderedDict()
    # from .utils import theano_print_values
    for param, grad in zip(params, grads):
        value = param.get_value(borrow=True)
        l = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                          broadcastable=param.broadcastable)
        g = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                          broadcastable=param.broadcastable)
        r = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                          broadcastable=param.broadcastable)
        s = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                          broadcastable=param.broadcastable)
        # grad = theano_print_values(grad, "grad_" + param.name)
        l_t = T.maximum(l * 0.999, T.abs_(grad))
        # l_t = theano_print_values(l_t, "l_" + param.name)
        g_t = g + T.abs_(grad)
        r_t = T.clip(r - param * grad, 0, 1000)
        s_t = s + grad
        factor = s_t / (l_t * T.maximum(g_t + l_t, alpha * l_t) + epsilon)
        # if not use_sigmoid:
        # else:
        #     factor = 2 * T.nnet.sigmoid(2 * s_t / (T.maximum(g_t + l_t, alpha * l_t) + epsilon)) - 1
        #     factor /= (l_t + epsilon)
        updates[param] = param - factor * (l_t + r_t)
    return updates


def yellow_fin(loss_or_grads, params, beta=0.9,
               learning_rate_init=0.01, momentum_init=0.0,
               t=None, window_width=20, debug=False):
    grads = get_or_compute_grads(loss_or_grads, params)
    updates = OrderedDict()

    alpha = theano.shared(utils.floatX(np.asarray(learning_rate_init)), name="learning_rate")
    mu = theano.shared(utils.floatX(np.asarray(momentum_init)), name="momentum")
    if t is None:
        t = theano.shared(np.asarray(0).astype(np.int32), name="t")
        updates[t] = t + 1

    h_max, h_min = curvature_range(grads, beta, t, updates, window_width)
    c = gradient_variance(grads, params, beta, updates)
    d = distance_to_optim(grads, beta, updates)
    if debug:
        h_max = utils.theano_print_values(h_max, "h_max")
        h_min = utils.theano_print_values(h_min, "h_min")
        c = utils.theano_print_values(c, "c")
        d = utils.theano_print_values(d, "d")
    # We have the equation x^2 D^2 + (1-x)^4 * C / h_min^2
    # where x = sqrt(mu)
    # Minimising this reduces to solving
    # y^3 + p * y + p = 0
    # y = x - 1
    # p = (D^2 h_min^2)/(2 * C)
    p = (T.sqr(d) * T.sqr(h_min)) / (2 * c)
    # root for w^3
    w3 = p * (T.sqrt(0.25 + p / 27.0) - 0.5)
    if debug:
        p = utils.theano_print_values(p, "p")
        w3 = utils.theano_print_values(w3, "w3")
    w = T.power(w3, 1.0 / 3.0)
    y = w - p / (3 * w)
    mu_sqrt1 = y + 1
    if debug:
        mu_sqrt1 = utils.theano_print_values(mu_sqrt1, "mu_sqrt1")
    mu_sqrt2 = (T.sqrt(h_max) - T.sqrt(h_min)) / (T.sqrt(h_max) + T.sqrt(h_min))
    mu_sqrt = T.maximum(mu_sqrt1, mu_sqrt2)
    alpha_t = T.sqr(1 - mu_sqrt) / h_min
    mu_t = T.sqr(mu_sqrt)
    # mu_t = utils.theano_print_values(mu_t, "mu_t")
    # alpha_t = utils.theano_print_values(alpha_t, "alpha_t")
    updates[mu] = ema(beta, mu, mu_t)
    updates[alpha] = ema(beta, alpha, alpha_t)
    # apply momentum
    updates.update(momentum(grads, params, updates[alpha], updates[mu]))
    return updates


def curvature_range(grads, beta, t, updates, window_width=20, debug=False):
    # Update the window
    window = theano.shared(T.zeros((window_width, )).eval(), name="window")
    t_mod = T.mod_check(t, window_width)
    updates[window] = T.set_subtensor(window[t_mod], sum(T.sum(T.sqr(g)) for g in grads))
    if debug:
        updates[window] = utils.theano_print_values(updates[window], "window")
    # Get the h_max_t and h_min_t
    t = T.minimum(t + 1, window_width)
    h_max_t = T.max(updates[window][:t])
    h_min_t = T.min(updates[window][:t])
    # Update the moving averages
    h_max = theano.shared(utils.floatX(np.asarray(0.0)), name="h_max")
    h_min = theano.shared(utils.floatX(np.asarray(0.0)), name="h_min")
    updates[h_max] = ema(beta, h_max, h_max_t)
    updates[h_min] = ema(beta, h_min, h_min_t)
    return updates[h_max], updates[h_min]


def gradient_variance(grads, params, beta, updates):
    norm = 0
    for p, g in zip(params, grads):
        value = p.get_value()
        mom1 = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                             broadcastable=p.broadcastable)
        mom2 = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                             broadcastable=p.broadcastable)

        updates[mom1] = ema(beta, mom1, g)
        updates[mom2] = ema(beta, mom2, T.sqr(g))
        norm += T.sum(T.abs_(updates[mom2] - T.sqr(updates[mom1])))
    return norm


def distance_to_optim(grads, beta, updates):
    g = theano.shared(utils.floatX(np.asarray(1.0)), name="g")
    h = theano.shared(utils.floatX(np.asarray(1.0)), name="h")
    d = theano.shared(utils.floatX(np.asarray(1.0)), name="d")
    new_norm = sum(T.sum(T.sqr(g)) for g in grads)
    updates[g] = ema(beta, g, T.sqrt(new_norm))
    updates[h] = ema(beta, h, new_norm)
    updates[d] = ema(beta, d, updates[g] / updates[h])
    return updates[d]


def norm_constraint(tensor_var, max_norm, norm_axes=None, epsilon=1e-7):
    """Max weight norm constraints and gradient clipping

    This takes a TensorVariable and rescales it so that incoming weight
    norms are below a specified constraint value. Vectors violating the
    constraint are rescaled so that they are within the allowed range.

    Parameters
    ----------
    tensor_var : TensorVariable
        Theano expression for update, gradient, or other quantity.
    max_norm : scalar
        This value sets the maximum allowed value of any norm in
        `tensor_var`.
    norm_axes : sequence (list or tuple)
        The axes over which to compute the norm.  This overrides the
        default norm axes defined for the number of dimensions
        in `tensor_var`. When this is not specified and `tensor_var` is a
        matrix (2D), this is set to `(0,)`. If `tensor_var` is a 3D, 4D or
        5D tensor, it is set to a tuple listing all axes but axis 0. The
        former default is useful for working with dense layers, the latter
        is useful for 1D, 2D and 3D convolutional layers.
        (Optional)
    epsilon : scalar, optional
        Value used to prevent numerical instability when dividing by
        very small or zero norms.

    Returns
    -------
    TensorVariable
        Input `tensor_var` with rescaling applied to weight vectors
        that violate the specified constraints.

    Examples
    --------
    >>> param = theano.shared(
    ...     np.random.randn(100, 200).astype(theano.config.floatX))
    >>> update = param + 100
    >>> update = norm_constraint(update, 10)
    >>> func = theano.function([], [], updates=[(param, update)])
    >>> # Apply constrained update
    >>> _ = func()
    >>> from lasagne.utils import compute_norms
    >>> norms = compute_norms(param.get_value())
    >>> np.isclose(np.max(norms), 10)
    True

    Notes
    -----
    When `norm_axes` is not specified, the axes over which the norm is
    computed depend on the dimensionality of the input variable. If it is
    2D, it is assumed to come from a dense layer, and the norm is computed
    over axis 0. If it is 3D, 4D or 5D, it is assumed to come from a
    convolutional layer and the norm is computed over all trailing axes
    beyond axis 0. For other uses, you should explicitly specify the axes
    over which to compute the norm using `norm_axes`.
    """
    ndim = tensor_var.ndim

    if norm_axes is not None:
        sum_over = tuple(norm_axes)
    elif ndim == 2:  # DenseLayer
        sum_over = (0,)
    elif ndim in [3, 4, 5]:  # Conv{1,2,3}DLayer
        sum_over = tuple(range(1, ndim))
    else:
        raise ValueError(
            "Unsupported tensor dimensionality {}."
            "Must specify `norm_axes`".format(ndim)
        )

    dtype = np.dtype(theano.config.floatX).type
    norms = T.sqrt(T.sum(T.sqr(tensor_var), axis=sum_over, keepdims=True))
    target_norms = T.clip(norms, 0, dtype(max_norm))
    constrained_output = \
        (tensor_var * (target_norms / (dtype(epsilon) + norms)))

    return constrained_output


def total_norm_constraint(tensor_vars, max_norm, target_vars=None, return_norm=False):
    """Rescales a list of tensors based on their combined norm

    If the combined norm of the input tensors exceeds the threshold then all
    tensors are rescaled such that the combined norm is equal to the threshold.

    Scaling the norms of the gradients is often used when training recurrent
    neural networks [1]_.

    Parameters
    ----------
    tensor_vars : List of TensorVariables.
        Tensors to be rescaled.
    max_norm : float
        Threshold value for total norm.
    target_vars : List of TensorVariables or None
        If not None the norm is computed as the inner product of tensor_vars and target_vars.
    return_norm : bool
        If true the total norm is also returned.

    Returns
    -------
    tensor_vars_scaled : list of TensorVariables
        The scaled tensor variables.
    norm : Theano scalar
        The combined norms of the input variables prior to rescaling,
        only returned if ``return_norms=True``.

    Examples
    --------
    >>> from lasagne.layers import InputLayer, DenseLayer
    >>> import lasagne
    >>> from lasagne.updates import sgd, total_norm_constraint
    >>> x = T.matrix()
    >>> y = T.ivector()
    >>> l_in = InputLayer((5, 10))
    >>> l1 = DenseLayer(l_in, num_units=7, nonlinearity=T.nnet.softmax)
    >>> output = lasagne.layers.get_output(l1, x)
    >>> cost = T.mean(T.nnet.categorical_crossentropy(output, y))
    >>> all_params = lasagne.layers.get_all_params(l1)
    >>> all_grads = T.grad(cost, all_params)
    >>> scaled_grads = total_norm_constraint(all_grads, 5)
    >>> updates = sgd(scaled_grads, all_params, learning_rate=0.1)

    Notes
    -----
    The total norm can be used to monitor training.

    References
    ----------
    .. [1] Sutskever, I., Vinyals, O., & Le, Q. V. (2014): Sequence to sequence
       learning with neural networks. In Advances in Neural Information
       Processing Systems (pp. 3104-3112).
    """
    if target_vars is None:
        norm = T.sqrt(sum(T.sum(tensor**2) for tensor in tensor_vars))
        multiplier = T.minimum(T.constant(1), max_norm / norm)
    else:
        norm = T.sqrt(sum(T.sum(tensor * target) for tensor, target in zip(tensor_vars, target_vars)))
        multiplier = T.minimum(T.constant(1), T.sqr(max_norm / norm))
    tensor_vars_scaled = [step*multiplier for step in tensor_vars]

    if return_norm:
        return tensor_vars_scaled, norm
    else:
        return tensor_vars_scaled


def apply_burnout(long_updates, initial_updates, burnout=0):
    if burnout > 0:
        t = theano.shared(np.asarray(0, dtype="int64"), name="momentum_t")
        cond = T.ge(t, burnout)
        initial_values = [initial_updates.get(k, k) for k, v in long_updates.items()]
        long_values = [v for _, v in long_updates.items()]
        values = ifelse(cond, long_values, initial_values)
        updates = OrderedDict([k, v] for (k, _), v in zip(long_updates.items(), values))
        updates[t] = t + T.constant(1)
        return updates
    else:
        return long_updates


def wrap_with_burnout(updates_fn, burnout=0):
    def get_updates(loss_or_grads, params, learning_rate, **kwargs):
        sgd_updates = sgd(loss_or_grads, params, learning_rate)
        updates = updates_fn(loss_or_grads, params, learning_rate, **kwargs)
        updates = apply_burnout(updates, sgd_updates, burnout)
        return updates
    return get_updates
