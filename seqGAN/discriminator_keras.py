import tensorflow as tf
import numpy as np
from keras.preprocessing.text import Tokenizer
from keras.preprocessing.sequence import pad_sequences
from keras.layers import core, Dense, Input, LSTM, Embedding, Dropout, Activation, Conv2D, MaxPooling2D, Flatten
from keras import backend
from keras.layers.merge import concatenate
from keras.models import Model
from keras.layers.normalization import BatchNormalization
from keras.callbacks import EarlyStopping, ModelCheckpoint
from keras import regularizers


# An alternative to tf.nn.rnn_cell._linear function, which has been removed in Tensorfow 1.0.1
# The highway layer is borrowed from https://github.com/mkroutikov/tf-lstm-char-cnn
def linear(input_, output_size, scope=None):
    '''
    Linear map: output[k] = sum_i(Matrix[k, i] * input_[i] ) + Bias[k]
    Args:
      input_: a tensor or a list of 2D, batch x n, Tensors.
      output_size: int, second dimension of W[i].
    scope: VariableScope for the created subgraph; defaults to "Linear".
      Returns:
      A 2D Tensor with shape [batch x output_size] equal to
      sum_i(input_[i] * W[i]), where W[i]s are newly created matrices.
    Raises:
      ValueError: if some of the arguments has unspecified or wrong shape.
    '''

    shape = input_.get_shape().as_list()
    if len(shape) != 2:
        raise ValueError("Linear is expecting 2D arguments: %s" % str(shape))
    if not shape[1]:
        raise ValueError("Linear expects shape[1] of arguments: %s" % str(shape))
    input_size = shape[1]

    # Now the computation.
    with tf.variable_scope(scope or "SimpleLinear"):
        matrix = tf.get_variable("Matrix", [output_size, input_size], dtype=input_.dtype)
        bias_term = tf.get_variable("Bias", [output_size], dtype=input_.dtype)

    return tf.matmul(input_, tf.transpose(matrix)) + bias_term


def highway(input_, size, num_layers=1, bias=-2.0, f=tf.nn.relu, scope='Highway'):
    """Highway Network (cf. http://arxiv.org/abs/1505.00387).
    t = sigmoid(Wy + b)
    z = t * g(Wy + b) + (1 - t) * y
    where g is nonlinearity, t is transform gate, and (1 - t) is carry gate.
    """

    with tf.variable_scope(scope):
        for idx in range(num_layers):
            g = f(linear(input_, size, scope='highway_lin_%d' % idx))

            t = tf.sigmoid(linear(input_, size, scope='highway_gate_%d' % idx) + bias)

            output = t * g + (1. - t) * input_
            input_ = output

    return output


class Discriminator(object):
    """
    A CNN for text classification.
    Uses an embedding layer, followed by a convolutional, max-pooling and softmax layer.
    Args:
      sequence_length: the length of a sequence
      num_classes: the dimensionality of output vector, i.e., number of classes
      vocab_size: the number of vocabularies
      embedding_size: the dimensionality of an embedding vector
      filter_sizes: filter size
      num_filters: number of filter
      l2_reg_lambda: lambda, a parameter for L2 regularizer
    """

    def __init__(self, sequence_length, num_classes, vocab_size,
                 embedding_size, filter_sizes, num_filters, l2_reg_lambda=0.0):
        # Placeholders for input, output and dropout
        self.input_x = tf.placeholder(tf.int32, [None, sequence_length], name="input_x")
        self.input_y = tf.placeholder(tf.float32, [None, num_classes], name="input_y")
        self.dropout_keep_prob = tf.placeholder(tf.float32, name="dropout_keep_prob")

        # Keeping track of l2 regularization loss (optional)
        l2_loss = tf.constant(0.0)

        with tf.variable_scope('discriminator'):
            #
            # Embedding layer
            #
            # ---------------- keras version ----------------
            MAX_SEQUENCE_LENGTH = 30
            self.W = tf.Variable(tf.random_uniform([vocab_size, embedding_size], -1.0, 1.0), name="W")
            self.W = Embedding(vocab_size,
                               embedding_size,
                               weights=[tf.random_uniform([vocab_size, embedding_size], -1.0, 1.0)],
                               input_length=MAX_SEQUENCE_LENGTH,
                               trainable=False)
            self.embedded_chars = self.W(self.input)
            self.embedded_chars_expanded = backend.expand_dims(self.embedded_chars, -1)
            # -----------------------------------------------

            #
            # Create a convolution + maxpool layer for each filter size
            #
            pooled_outputs = []
            for filter_size, num_filter in zip(filter_sizes, num_filters):
                with tf.name_scope("conv-maxpool-%s" % filter_size):
                    # Convolution Layer
                    conv = Conv2D(filters=num_filter,
                                  kernel_size=filter_size,
                                  padding='valid',
                                  activation="relu",
                                  strides=1)(self.embedded_chars_expanded)
                    # Max-pooling over the outputs
                    conv = MaxPooling2D(pool_size=2)(conv)
                    pooled_outputs.append(conv)
            #
            # Combine all the pooled features
            #
            num_filters_total = sum(num_filters)
            self.h_pool = concatenate(pooled_outputs, 3)
            self.h_pool_flat = backend.reshape(self.h_pool, [-1, num_filters_total])

            #
            # Add highway
            #
            self.h_highway = highway(self.h_pool_flat, self.h_pool_flat.get_shape()[1], 1, 0)

            #
            # Add dropout
            #
            self.h_drop = Dropout(self.dropout_keep_prob)(self.h_highway)

            #
            # Final (unnormalized) scores and predictions
            #
            self.scores = BatchNormalization()(self.h_drop)
            self.scores = Dense(num_classes,
                                activation='softmax',
                                kernel_regularizer=regularizers.l2(0.01),
                                activity_regularizer=regularizers.l1(0.01))(self.h_drop)

            with tf.name_scope("output"):
                W = tf.Variable(tf.truncated_normal([num_filters_total, num_classes], stddev=0.1), name="W")
                b = tf.Variable(tf.constant(0.1, shape=[num_classes]), name="b")
                l2_loss += tf.nn.l2_loss(W)
                l2_loss += tf.nn.l2_loss(b)
                self.scores = tf.nn.xw_plus_b(self.h_drop, W, b, name="scores")
                self.ypred_for_auc = tf.nn.softmax(self.scores)
                self.predictions = tf.argmax(self.scores, 1, name="predictions")

            # CalculateMean cross-entropy loss
            with tf.name_scope("loss"):
                losses = tf.nn.softmax_cross_entropy_with_logits(logits=self.scores, labels=self.input_y)
                self.loss = tf.reduce_mean(losses) + l2_reg_lambda * l2_loss

        self.params = [param for param in tf.trainable_variables() if 'discriminator' in param.name]
        d_optimizer = tf.train.AdamOptimizer(1e-4)
        grads_and_vars = d_optimizer.compute_gradients(self.loss, self.params, aggregation_method=2)
        self.train_op = d_optimizer.apply_gradients(grads_and_vars)
