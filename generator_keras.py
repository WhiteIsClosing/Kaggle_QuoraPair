import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops


class Generator(object):
    def __init__(self, num_emb, batch_size, embed_dim, hidden_dim,
                 sequence_length, start_token,
                 learning_rate=0.01, reward_gamma=0.95):
        self.num_vocab = num_emb
        self.batch_size = batch_size
        self.embed_dim = embed_dim
        self.hidden_dim = hidden_dim
        self.sequence_length = sequence_length
        self.start_token = tf.constant([start_token] * self.batch_size, dtype=tf.int32)
        self.learning_rate = tf.Variable(float(learning_rate), trainable=False)
        self.reward_gamma = reward_gamma
        self.g_params = []
        self.d_params = []
        self.temperature = 1.0
        self.grad_clip = 5.0

        self.expected_reward = tf.Variable(tf.zeros([self.sequence_length]))

        with tf.variable_scope('generator'):
            self.g_embeddings = tf.Variable(self.init_matrix([self.num_vocab, self.embed_dim]))
            self.g_params.append(self.g_embeddings)

            # maps h_tm1 to h_t for generator
            self.g_recurrent_unit = self.create_recurrent_unit(self.g_params)

            # maps h_t to o_t (output token logits)
            self.g_output_unit = self.create_output_unit(self.g_params)
        #
        # placeholder definition
        # ----------------------------------------------------------------------------
        # sequence of tokens generated by generator
        self.x = tf.placeholder(tf.int32, shape=[self.batch_size, self.sequence_length])
        # get from rollout policy and discriminator
        self.rewards = tf.placeholder(tf.float32, shape=[self.batch_size, self.sequence_length])

        #
        # processed for batch
        # ----------------------------------------------------------------------------
        with tf.device("/cpu:0"):
            # dim(self.processed_x) =  (seq_length, batch_size, embed_dim)
            self.processed_x = tf.transpose(tf.nn.embedding_lookup(self.g_embeddings, self.x), perm=[1, 0, 2])

        #
        # Initial states
        # ----------------------------------------------------------------------------
        self.h0 = tf.zeros([self.batch_size, self.hidden_dim])
        self.h0 = tf.stack([self.h0, self.h0])

        gen_o = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)
        gen_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)

        #
        # Forward prediction to predict the sequence from (t+1) to T (predict by prediction)
        # ----------------------------------------------------------------------------
        def _g_recurrence(i, x_t, h_tm1, gen_o, gen_x):
            # Def:
            #   LSTM forward operation unit, where output at (t-1) will be sent as input at t
            #   This function is used prediction time slice from (t+1) to T
            # Args ------------
            #   i: counter
            #   x_t: input at time t
            #   h_tm1: a tensor that packs [prev_hidden_state, prev_c], i.e., h_{t-1}
            #   gen_o:
            #   gen_x: to record each predicted input from t to T
            # Returns ------------
            #   i + 1: next counter
            #   x_tp1: input at time (t+1), i.e., x_{t+1}, which is from next_token, the output from o_t
            #   h_t: a tensor that packs [now_hidden_state, now_c], i.e., h_{t}
            #   gen_o:
            #   gen_x: add next_token to the list, i.e., to record each predicted input from (t+1) to T

            # hidden_memory_tuple
            # h_tm1: the previous tensor that packs [prev_hidden_state, prev_c]
            # h_t: the current tensor that packs [now_hidden_state, now_c]
            h_t = self.g_recurrent_unit(x_t, h_tm1)

            # dim(o_t) = (batch_size, num_vocab), logits not prob
            # h_t: the current tensor that packs [now_hidden_state, now_c]
            # o_t: the output of LSTM at time t
            o_t = self.g_output_unit(h_t)

            log_prob = tf.log(tf.nn.softmax(o_t))
            next_token = tf.cast(tf.reshape(tf.multinomial(log_prob, 1), [self.batch_size]), tf.int32)

            # Convert next_token (vocabularies) to embeddings (next input, i.e., x_{t+1})
            # dim(x_tp1) = (batch_size, embed_dim)
            x_tp1 = tf.nn.embedding_lookup(self.g_embeddings, next_token)

            # dim(gen_o) = (batch_size, num_vocab), prob. dist. on vocab. vector
            # e.g., [3, 2, 1] == softmax ==> [0.665, 0.244, 0.09] == * one_hot ==> [0.665, 0, 0]
            # reduce_sum(input_tensor, axis=1), row-wise summation
            tmp = tf.multiply(tf.one_hot(next_token, self.num_vocab, 1.0, 0.0), tf.nn.softmax(o_t))
            gen_o = gen_o.write(i, tf.reduce_sum(tmp, axis=1))

            # [indices, batch_size]
            gen_x = gen_x.write(i, next_token)

            return i + 1, x_tp1, h_t, gen_o, gen_x

        _, _, _, self.gen_o, self.gen_x = control_flow_ops.while_loop(cond=lambda i, _1, _2, _3, _4: i < self.sequence_length,
                                                                      body=_g_recurrence, # forward prediction
                                                                      loop_vars=(tf.constant(0, dtype=tf.int32),
                                                                                 tf.nn.embedding_lookup(self.g_embeddings, self.start_token),
                                                                                 self.h0, gen_o, gen_x))

        # dim(self.gen_x) = (seq_length, batch_size)
        self.gen_x = self.gen_x.stack()

        # dim(self.gen_x) = (batch_size, seq_length)
        self.gen_x = tf.transpose(self.gen_x, perm=[1, 0])

        # supervised pre-training for generator
        g_predictions = tensor_array_ops.TensorArray(dtype=tf.float32,
                                                     size=self.sequence_length,
                                                     dynamic_size=False,
                                                     infer_shape=True)

        ta_emb_x = tensor_array_ops.TensorArray(dtype=tf.float32,
                                                size=self.sequence_length)
        ta_emb_x = ta_emb_x.unstack(self.processed_x)


        #
        # Forward prediction to predict the sequence from 0 to t (predict by known instances)
        # ----------------------------------------------------------------------------
        def _pretrain_recurrence(i, x_t, h_tm1, g_predictions):
            # Def:
            #   LSTM forward operation unit, given input and output
            #   This function is used prediction time slice from 1 to t
            # Args ------------
            #   i: counter
            #   x_t: input at time t
            #   h_tm1: a tensor that packs [prev_hidden_state, prev_c], i.e., h_{t-1}
            #   g_predictions: add softmax(o_t) to the list, i.e., to record each predicted input from t to T
            # Returns ------------
            #   i + 1: next counter
            #   x_tp1: input at time (t+1), i.e., x_{t+1}, which is read from ta_emb_x
            #   h_t: a tensor that packs [now_hidden_state, now_c], i.e., h_{t}
            #   g_predictions: add softmax(o_t) to the list, i.e., to record each predicted input from t to T

            #   h_tm1: the previous tensor that packs [prev_hidden_state, prev_c]
            #   h_t: the current tensor that packs [now_hidden_state, now_c]
            h_t = self.g_recurrent_unit(x_t, h_tm1)

            # LSTM output
            # h_t: the current tensor that packs [now_hidden_state, now_c]
            # o_t: the output of LSTM at time t
            o_t = self.g_output_unit(h_t)

            # batch x vocab_size
            g_predictions = g_predictions.write(i, tf.nn.softmax(o_t))

            x_tp1 = ta_emb_x.read(i)
            return i + 1, x_tp1, h_t, g_predictions

        _, _, _, self.g_predictions = control_flow_ops.while_loop(cond=lambda i, _1, _2, _3: i < self.sequence_length,
                                                                  body=_pretrain_recurrence, # forward prediction
                                                                  loop_vars=(tf.constant(0, dtype=tf.int32),
                                                                             tf.nn.embedding_lookup(self.g_embeddings, self.start_token),
                                                                             self.h0, g_predictions))
        # dim(self.g_predictions) = (batch_size, seq_length, vocab_size)
        self.g_predictions = tf.transpose(self.g_predictions.stack(), perm=[1, 0, 2])

        #
        # Pre-training loss
        # ----------------------------------------------------------------------------
        self.pretrain_loss = -tf.reduce_sum(tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])),
                                                       self.num_vocab, 1.0, 0.0)
                                            * tf.log(tf.clip_by_value(tf.reshape(self.g_predictions, [-1, self.num_vocab]), 1e-20, 1.0))
                                            ) / (self.sequence_length * self.batch_size)

        #
        # Training updates
        # ----------------------------------------------------------------------------
        pretrain_opt = self.g_optimizer(self.learning_rate)

        # Compute the gradient by using self.pretrain_loss and self.g_params
        # Clip the gradient to a finite range self.grad_clip
        self.pretrain_grad, _ = tf.clip_by_global_norm(tf.gradients(self.pretrain_loss, self.g_params), self.grad_clip)

        # Update parameters by using self.pretrain_grad and self.g_params
        self.pretrain_updates = pretrain_opt.apply_gradients(zip(self.pretrain_grad, self.g_params))

        #
        # Unsupervised Training
        # ----------------------------------------------------------------------------
        self.g_loss = -tf.reduce_sum(tf.reduce_sum(tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])),
                                                              self.num_vocab, 1.0, 0.0) *
                                                   tf.log(tf.clip_by_value(tf.reshape(self.g_predictions,
                                                                                      [-1, self.num_vocab]), 1e-20, 1.0)), 1) *
                                     tf.reshape(self.rewards, [-1]))

        # Set the optimizer (default is AdamOptimizer)
        g_opt = self.g_optimizer(self.learning_rate)

        # Compute the gradient by using self.g_loss and self.g_params
        # Clip the gradient to a finite range self.grad_clip
        self.g_grad, _ = tf.clip_by_global_norm(tf.gradients(self.g_loss, self.g_params), self.grad_clip)

        # Update parameters by using self.g_grad and self.g_params
        self.g_updates = g_opt.apply_gradients(zip(self.g_grad, self.g_params))

    def generate(self, sess):
        outputs = sess.run(self.gen_x)
        return outputs

    def pretrain_step(self, sess, x):
        outputs = sess.run([self.pretrain_updates, self.pretrain_loss], feed_dict={self.x: x})
        return outputs

    def init_matrix(self, shape):
        return tf.random_normal(shape, stddev=0.1)

    def init_vector(self, shape):
        return tf.zeros(shape)

    def create_recurrent_unit(self, params):
        # Define Weights and Bias for input and hidden tensor
        self.Wi = tf.Variable(self.init_matrix([self.embed_dim, self.hidden_dim]))
        self.Ui = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bi = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wf = tf.Variable(self.init_matrix([self.embed_dim, self.hidden_dim]))
        self.Uf = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bf = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wog = tf.Variable(self.init_matrix([self.embed_dim, self.hidden_dim]))
        self.Uog = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bog = tf.Variable(self.init_matrix([self.hidden_dim]))

        self.Wc = tf.Variable(self.init_matrix([self.embed_dim, self.hidden_dim]))
        self.Uc = tf.Variable(self.init_matrix([self.hidden_dim, self.hidden_dim]))
        self.bc = tf.Variable(self.init_matrix([self.hidden_dim]))

        params.extend([self.Wi, self.Ui, self.bi,
                       self.Wf, self.Uf, self.bf,
                       self.Wog, self.Uog, self.bog,
                       self.Wc, self.Uc, self.bc])

        def unit(x, hidden_memory_tm1):
            #
            # Define LSTM transition operation
            # Args:
            #   x: input
            #   hidden_memory_tm1: the previous tensor that packs [prev_hidden_state, prev_c]
            # Returns:
            #   a current tensor that packs [now_hidden_state, now_c]

            previous_hidden_state, c_prev = tf.unstack(hidden_memory_tm1)

            # Input Gate
            i = tf.sigmoid(tf.matmul(x, self.Wi) +
                           tf.matmul(previous_hidden_state, self.Ui) + self.bi)

            # Forget Gate
            f = tf.sigmoid(tf.matmul(x, self.Wf) +
                           tf.matmul(previous_hidden_state, self.Uf) + self.bf)

            # Output Gate
            o = tf.sigmoid(tf.matmul(x, self.Wog) +
                           tf.matmul(previous_hidden_state, self.Uog) + self.bog)

            # New Memory Cell
            c_ = tf.nn.tanh(tf.matmul(x, self.Wc) +
                            tf.matmul(previous_hidden_state, self.Uc) + self.bc)

            # Final Memory cell
            c = f * c_prev + i * c_

            # Current Hidden state
            current_hidden_state = o * tf.nn.tanh(c)

            return tf.stack([current_hidden_state, c])

        return unit

    def create_output_unit(self, params):
        self.Wo = tf.Variable(self.init_matrix([self.hidden_dim, self.num_vocab]))
        self.bo = tf.Variable(self.init_matrix([self.num_vocab]))
        params.extend([self.Wo, self.bo])

        def unit(hidden_memory_tuple):
            #
            # Define LSTM output operation
            # Args:
            #   hidden_memory_tuple: a tensor that packs [now_hidden_state, now_c]
            # Returns:
            #   logits: the output of LSTM at time t
            hidden_state, c_prev = tf.unstack(hidden_memory_tuple)

            # hidden_state : batch x hidden_dim
            logits = tf.matmul(hidden_state, self.Wo) + self.bo

            # output = tf.nn.softmax(logits)
            return logits

        return unit

    def g_optimizer(self, *args, **kwargs):
        return tf.train.AdamOptimizer(*args, **kwargs)