import os.path
import time
import numpy as np
import cupy
import random

from chainer import (Variable, Chain, serializers, optimizers, report)

import chainer.functions as F
import chainer.links as L
import chainer.initializers as I

from components.conv_gru import ConvGRU2D
from components.stateless_conv_gru import StatelessConvGRU2D
from components.embedding_conv2d import EmbeddingConv2D


class PredictiveAutoencoder(Chain):
    def __init__(self, environment):
        w = I.HeNormal()
        super(PredictiveAutoencoder, self).__init__()
        self.action_meanings = environment.unwrapped.get_action_meanings()
        self.action_space = 18
        with self.init_scope():
            self.conv1 = L.Convolution2D(
                in_channels=3,
                out_channels=16,
                ksize=8,
                stride=4,
                initialW=w,
            )
            self.embed_conv2d = EmbeddingConv2D(
                embed_size=self.action_space,
                in_channels=3,
                out_channels=3,
                ksize=8,
                stride=4,
                initialW=w,
            )
            self.conv2 = L.Convolution2D(
                in_channels=19,
                out_channels=32,
                ksize=4,
                stride=2,
                initialW=w,
            )
            self.conv3 = L.Convolution2D(
                32,
                out_channels=32,
                ksize=1,
                pad=0,
                initialW=w,
            )
            self.conv_gru1 = ConvGRU2D(
                32,
                out_channels=64,
                ksize=1,
                init=w,
                inner_init=w,
            )
            self.linear1 = L.Linear(
                None,
                256,
                initialW=w,
            )
            self.linear2 = L.Linear(
                256,
                out_size=self.action_space,
                initialW=w,
            )
            self.deconv1 = L.Deconvolution2D(
                64,
                out_channels=32,
                ksize=4,
                stride=2,
                outsize=(39, 51),
                initialW=w,
            )
            self.deconv2 = L.Deconvolution2D(
                32,
                out_channels=3,
                ksize=8,
                stride=4,
                outsize=(160, 210),
                initialW=w,
            )


    def __call__(self, x, action):
        h1 = F.relu(self.conv1(x))
        index = F.expand_dims(cupy.array(action, dtype="int32"), axis=0)
        h2 = F.relu(self.embed_conv2d(index, x))
        h = F.concat((h1, h2), axis=1)  # Glue together the action convolutions
        h = F.relu(self.conv2(h))
        h = F.relu(self.conv3(h))
        h = F.relu(self.conv_gru1(h))

        h_img = F.relu(self.deconv1(h))
        h_img = self.deconv2(h_img)

        h_action = F.relu(self.linear1(h))
        h_action = self.linear2(h_action)

        return h_img, h_action


class Classifier(Chain):
    def __init__(self, predictor, weight=0.75):
        super(Classifier, self).__init__()
        with self.init_scope():
            self.predictor = predictor
            self.weight = float(weight)
            self.y_image = None
            self.y_action = None
            self.image_mask = None

    def action_meaning(self, act):
        if act >= len(self.predictor.action_meanings):
            return 'NOOP({})'.format(act)
        else:
            return self.predictor.action_meanings[act]

    def __call__(self, x_image, t_image, x_action, t_action):
        self.y_image, self.y_action = self.predictor(x_image, x_action)

        predicted_action = self.action_meaning(
            F.argmax(self.y_action, axis=1).data[0])
        real_action = self.action_meaning(t_action)
        if predicted_action != real_action:
            print("Predicted action:", predicted_action,
                  "it was actually", real_action)
        image_loss = F.mean_squared_error(self.y_image, t_image)
        self.error_mask = normalize_2d(F.squared_error(self.y_image, t_image))
        action_loss = F.softmax_cross_entropy(
            self.y_action,
            F.expand_dims(cupy.array(t_action, dtype="int32"), axis=0),
        )
        print('Image loss', image_loss.data, ', Action loss:', action_loss.data)
        return self.weight * image_loss + (1.0 - self.weight) * action_loss


class PredictorAgent(object):
    def __init__(self, save_dir, environment,
                 name=None,
                 load_saved=True,
                 classifier_weight=0.5,
                 backprop_rounds=10,
                 gpu=-1):
        self.name = name or 'predictive_autoencoder'
        self.save_dir = save_dir
        self.backprop_rounds = backprop_rounds

        self.model = PredictiveAutoencoder(
            # TODO: the environment shouldn't be passed in obviously
            # We should own the action space descriptions or pull out
            # all code that needs the descriptions
            environment=environment,
        )

        model = self.model
        if (gpu > 0):
            model = model.to_gpu(gpu)
        self.classifier = Classifier(model, weight=classifier_weight)

        self.optimizer = optimizers.Adam()
        self.optimizer.setup(self.model)

        # Trainer state
        self.last_action = 0
        self.last_obs = None
        self.i = 0
        self._predicted_image = None
        self.predicted_action = None
        self.loss = 0

        if load_saved and os.path.exists(self._model_filename()):
            self.load(self.save_dir)

    def initialize_state(self, first_obs):
        self.last_obs = first_obs

    def _model_filename(self):
        return self.save_dir + self.name + '.model'

    def _opti_filename(self):
        return self.save_dir + self.name + '.optimizer'

    def save(self):
        # TODO: use hd5 groups to put everything in one file
        # TODO: serialize the classifier, the weight is being lost
        serializers.save_hdf5(self._model_filename(), self.model)
        serializers.save_hdf5(self._opti_filename(), self.optimizer)
        return time.time()

    def load(self, save_dir):
        serializers.load_hdf5(self._model_filename(), self.model)
        serializers.load_hdf5(self._opti_filename(), self.optimizer)

    @property
    def predicted_image(self):
        # The transpose is because OpenAI gym and chainer have
        # different depth conventions
        return F.transpose(self._predicted_image[0])

    @property
    def error_mask(self):
        return F.transpose(self.attention_mask)

    def describe_model(self):
        # TODO: fix this method
        graph = cg.build_computational_graph(model)

        dot_name = DIAGRAM_DIR + model_name + '.dot'
        png_name = DIAGRAM_DIR + model_name + '.png'

        with open(dot_name, 'w') as o:
            o.write(graph.dump())

            subprocess.call(['dot', '-Tpng', dot_name, '-o', png_name])


    def __call__(self, obs, reward):
        '''Takes in observation (processed) and returns the next action to
        execute'''
        self.i += 1
        # policy!
        action = random.randint(1, 6)

        self.loss += self.classifier(self.last_obs, obs, self.last_action, action)
        self._predicted_image = self.classifier.y_image
        self.predicted_action = self.classifier.y_action
        self.attention_mask = self.classifier.error_mask

        self.last_action = action
        self.last_obs = obs

        if self.i % self.backprop_rounds == 0:
            self.model.cleargrads()
            self.loss.backward()
            self.loss.unchain_backward()
            self.optimizer.update()
            report({'loss': self.loss})
        return action

def to_one_hot(size, index):
    '''Converts an int into a one-hot array'''
    arr = cupy.zeros(size, dtype="int32")
    arr[index] = 1
    return arr


def normalize_2d(x):
    exp = F.exp(x[0])
    sums = F.sum(F.sum(exp, axis=-1), axis=-1)
    expanded = F.expand_dims(F.expand_dims(sums, axis=-1), axis=-1)
    denominator = F.tile(expanded, (1, 160, 210))
    return exp / denominator
