# -*- coding: utf-8 -*-
"""
Train an Auxiliary Classifier Generative Adversarial Network (ACGAN) on the
MNIST dataset. See https://arxiv.org/abs/1610.09585 for more details.

You should start to see reasonable images after ~5 epochs, and good images
by ~15 epochs. You should use a GPU, as the convolution-heavy operations are
very slow on the CPU. Prefer the TensorFlow backend if you plan on iterating,
as the compilation time can be a blocker using Theano.

Timings:

Hardware           | Backend | Time / Epoch
-------------------------------------------
 CPU               | TF      | 3 hrs
 Titan X (maxwell) | TF      | 4 min
 Titan X (maxwell) | TH      | 7 min

Consult https://github.com/lukedeo/keras-acgan for more information and
example output
"""
from __future__ import print_function

from collections import defaultdict
try:
    import cPickle as pickle
except ImportError:
    import pickle
from PIL import Image

from six.moves import range

from keras import layers
from keras.layers import Input, Dense, Reshape, Flatten, Embedding, Dropout
from keras.layers import BatchNormalization
from keras.layers.advanced_activations import LeakyReLU
from keras.layers.convolutional import Conv2DTranspose, Conv2D
from keras.models import Sequential, Model
from keras.optimizers import Adam
from keras.utils.generic_utils import Progbar
import numpy as np

import os
import matplotlib.pyplot as plt
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score
from sklearn.utils import shuffle

num_classes = 2
images_dir = 'images128'

def get_data():
    x_melanoma_train, y_melanoma_train = get_dir_data(os.path.join('.', images_dir, 'train', 'melanoma'), 1)
    x_melanoma_test, y_melanoma_test = get_dir_data(os.path.join('.', images_dir, 'validation', 'melanoma'), 1)
    x_other_train, y_other_train = get_dir_data(os.path.join('.', images_dir, 'train', 'outros'), 0)
    x_other_test, y_other_test = get_dir_data(os.path.join('.', images_dir, 'validation', 'outros'), 0)

    x_train = np.concatenate((x_melanoma_train, x_other_train))
    y_train = np.concatenate((y_melanoma_train, y_other_train))

    x_test = np.concatenate((x_melanoma_test, x_other_test))
    y_test = np.concatenate((y_melanoma_test, y_other_test))

    x_train, ytrain = shuffle(x_train, y_train)
    x_test, y_test = shuffle(x_test, y_test)

    return x_train, y_train, x_test, y_test

def get_dir_data(path, clz):
    x = [os.path.join(path, f) for f in os.listdir(path)]
    x = np.array([np.array(Image.open(fname)) for fname in x])
    y = np.ones((len(x), ))*clz
    return x, y


def build_generator(latent_size):
    # we will map a pair of (z, L), where z is a latent vector and L is a
    # label drawn from P_c, to image space (..., 28, 28, 1)
    cnn = Sequential()

    cnn.add(Dense(12 * 12 * 768, input_dim=latent_size, activation='relu'))
    cnn.add(Reshape((12, 12, 768)))

    # upsample to (16, 16, ...)
    cnn.add(Conv2DTranspose(384, 5, strides=1, padding='valid',
                            activation='relu',
                            kernel_initializer='glorot_normal'))
    cnn.add(BatchNormalization())

    # upsample to (32, 32, ...)
    cnn.add(Conv2DTranspose(192, 5, strides=2, padding='same',
                            activation='relu',
                            kernel_initializer='glorot_normal'))
    cnn.add(BatchNormalization())

    # upsample to (64, 64, ...)
    cnn.add(Conv2DTranspose(96, 5, strides=2, padding='same',
                            activation='relu',
                            kernel_initializer='glorot_normal'))
    cnn.add(BatchNormalization())

    # upsample to (128, 128, ...)
    cnn.add(Conv2DTranspose(3, 5, strides=2, padding='same',
                            activation='tanh',
                            kernel_initializer='glorot_normal'))

    # this is the z space commonly referred to in GAN papers
    latent = Input(shape=(latent_size, ))

    # this will be our label
    image_class = Input(shape=(1,), dtype='int32')

    cls = Flatten()(Embedding(num_classes, latent_size,
                              embeddings_initializer='glorot_normal')(image_class))

    # hadamard product between z-space and a class conditional embedding
    h = layers.multiply([latent, cls])

    fake_image = cnn(h)

    return Model([latent, image_class], fake_image)


def build_discriminator():
    # build a relatively standard conv net, with LeakyReLUs as suggested in
    # the reference paper
    cnn = Sequential()

    cnn.add(Conv2D(16, 3, padding='same', strides=2,
                   input_shape=(128, 128, 3)))
    cnn.add(LeakyReLU(0.2))
    cnn.add(Dropout(0.5))

    cnn.add(Conv2D(32, 3, padding='same', strides=1))
    cnn.add(LeakyReLU(0.2))
    cnn.add(Dropout(0.5))

    cnn.add(Conv2D(64, 3, padding='same', strides=2))
    cnn.add(LeakyReLU(0.2))
    cnn.add(Dropout(0.5))

    cnn.add(Conv2D(128, 3, padding='same', strides=1))
    cnn.add(LeakyReLU(0.2))
    cnn.add(Dropout(0.5))

    cnn.add(Conv2D(256, 3, padding='same', strides=2))
    cnn.add(LeakyReLU(0.2))
    cnn.add(Dropout(0.5))

    cnn.add(Conv2D(512, 3, padding='same', strides=1))
    cnn.add(LeakyReLU(0.2))
    cnn.add(Dropout(0.5))

    cnn.add(Flatten())

    image = Input(shape=(128, 128, 3))

    features = cnn(image)

    # first output (name=generation) is whether or not the discriminator
    # thinks the image that is being shown is fake, and the second output
    # (name=auxiliary) is the class that the discriminator thinks the image
    # belongs to.
    fake = Dense(1, activation='sigmoid', name='generation')(features)
    aux = Dense(num_classes, activation='softmax', name='auxiliary')(features)

    return Model(image, [fake, aux])

if __name__ == '__main__':

    # batch and latent size taken from the paper
    max_runs = 10
    epochs = 50000
    batch_size = 100
    latent_size = 110

    # Adam parameters suggested in https://arxiv.org/abs/1511.06434
    adam_lr = 0.0002
    adam_beta_1 = 0.5

    # build the discriminator
    print('Discriminator model:')
    discriminator = build_discriminator()
    discriminator.compile(
        optimizer=Adam(lr=adam_lr, beta_1=adam_beta_1),
        loss=['binary_crossentropy', 'sparse_categorical_crossentropy']
    )
    discriminator.summary()

    # build the generator
    generator = build_generator(latent_size)

    latent = Input(shape=(latent_size, ))
    image_class = Input(shape=(1,), dtype='int32')

    # get a fake image
    fake = generator([latent, image_class])

    # we only want to be able to train generation for the combined model
    discriminator.trainable = False
    fake, aux = discriminator(fake)
    combined = Model([latent, image_class], [fake, aux])

    print('Combined model:')
    combined.compile(
        optimizer=Adam(lr=adam_lr, beta_1=adam_beta_1),
        loss=['binary_crossentropy', 'sparse_categorical_crossentropy']
    )
    combined.summary()

    # get our mnist data, and force it to be of shape (..., 28, 28, 1) with
    # range [-1, 1]
    x_train, y_train, x_test, y_test = get_data()
    x_train = (x_train.astype(np.float32) - 127.5) / 127.5
    # x_train = np.expand_dims(x_train, axis=-1)

    x_test = (x_test.astype(np.float32) - 127.5) / 127.5
    # x_test = np.expand_dims(x_test, axis=-1)

    num_train, num_test = x_train.shape[0], x_test.shape[0]

    for nrun in range(1, max_runs+1):
        print('Run {}/{}'.format(nrun, max_runs))

        run_path = os.path.join('.', 'r{}'.format(nrun))
        os.mkdir(run_path)

        train_history = defaultdict(list)
        test_history = defaultdict(list)

        for epoch in range(1, epochs + 1):
            print('Epoch {}/{}'.format(epoch, epochs))

            num_batches = int(x_train.shape[0] / batch_size)

            # we don't want the discriminator to also maximize the classification
            # accuracy of the auxiliary classifier on generated images, so we
            # don't train discriminator to produce class labels for generated
            # images (see https://openreview.net/forum?id=rJXTf9Bxg).
            # To preserve sum of sample weights for the auxiliary classifier,
            # we assign sample weight of 2 to the real images.
            disc_sample_weight = [np.ones(2 * batch_size),
                                  np.concatenate((np.ones(batch_size) * 2,
                                                  np.zeros(batch_size)))]

            epoch_gen_loss = []
            epoch_disc_loss = []

            for index in range(num_batches):
                # generate a new batch of noise
                noise = np.random.uniform(-1, 1, (batch_size, latent_size))

                # get a batch of real images
                image_batch = x_train[index * batch_size:(index + 1) * batch_size]
                label_batch = y_train[index * batch_size:(index + 1) * batch_size]

                # sample some labels from p_c
                sampled_labels = np.random.randint(0, num_classes, batch_size)

                # generate a batch of fake images, using the generated labels as a
                # conditioner. We reshape the sampled labels to be
                # (batch_size, 1) so that we can feed them into the embedding
                # layer as a length one sequence
                generated_images = generator.predict(
                    [noise, sampled_labels.reshape((-1, 1))], verbose=0)

                x = np.concatenate((image_batch, generated_images))

                # use one-sided soft real/fake labels
                # Salimans et al., 2016
                # https://arxiv.org/pdf/1606.03498.pdf (Section 3.4)
                soft_zero, soft_one = 0, 0.95
                y = np.array([soft_one] * batch_size + [soft_zero] * batch_size)
                aux_y = np.concatenate((label_batch, sampled_labels), axis=0)

                # see if the discriminator can figure itself out...
                epoch_disc_loss.append(discriminator.train_on_batch(
                    x, [y, aux_y], sample_weight=disc_sample_weight))

                # make new noise. we generate 2 * batch size here such that we have
                # the generator optimize over an identical number of images as the
                # discriminator
                noise = np.random.uniform(-1, 1, (2 * batch_size, latent_size))
                sampled_labels = np.random.randint(0, num_classes, 2 * batch_size)

                # we want to train the generator to trick the discriminator
                # For the generator, we want all the {fake, not-fake} labels to say
                # not-fake
                trick = np.ones(2 * batch_size) * soft_one

                epoch_gen_loss.append(combined.train_on_batch(
                    [noise, sampled_labels.reshape((-1, 1))],
                    [trick, sampled_labels]))


            print('Testing for epoch {}:'.format(epoch))

            # evaluate the testing loss here

            # generate a new batch of noise
            noise = np.random.uniform(-1, 1, (num_test, latent_size))

            # sample some labels from p_c and generate images from them
            sampled_labels = np.random.randint(0, num_classes, num_test)
            generated_images = generator.predict(
                [noise, sampled_labels.reshape((-1, 1))], verbose=False)

            x = np.concatenate((x_test, generated_images))
            y = np.array([1] * num_test + [0] * num_test)
            aux_y = np.concatenate((y_test, sampled_labels), axis=0)

            # see if the discriminator can figure itself out...
            discriminator_test_loss = discriminator.evaluate(
                x, [y, aux_y], verbose=False)

            discriminator_train_loss = np.mean(np.array(epoch_disc_loss), axis=0)

            # make new noise
            noise = np.random.uniform(-1, 1, (2 * num_test, latent_size))
            sampled_labels = np.random.randint(0, num_classes, 2 * num_test)

            trick = np.ones(2 * num_test)

            generator_test_loss = combined.evaluate(
                [noise, sampled_labels.reshape((-1, 1))],
                [trick, sampled_labels], verbose=False)

            generator_train_loss = np.mean(np.array(epoch_gen_loss), axis=0)


            # Aqui começa a medir accuracy do discriminator
            y_pred = discriminator.predict(x_test)
            y_pred = np.argmax(y_pred[1], axis=1)
            discriminator_accuracy = accuracy_score(y_test, y_pred)


            # generate an epoch report on performance
            train_history['generator'].append(generator_train_loss)
            train_history['discriminator'].append(discriminator_train_loss)

            test_history['generator'].append(generator_test_loss)
            test_history['discriminator'].append(discriminator_test_loss)
            test_history['accuracy'].append(discriminator_accuracy)

            print('Discriminator accuracy: {}'.format(discriminator_accuracy))

            print('{0:<22s} | {1:4s} | {2:15s} | {3:5s}'.format(
                'component', *discriminator.metrics_names))
            print('-' * 65)

            ROW_FMT = '{0:<22s} | {1:<4.2f} | {2:<15.4f} | {3:<5.4f}'
            print(ROW_FMT.format('generator (train)',
                                 *train_history['generator'][-1]))
            print(ROW_FMT.format('generator (test)',
                                 *test_history['generator'][-1]))
            print(ROW_FMT.format('discriminator (train)',
                                 *train_history['discriminator'][-1]))
            print(ROW_FMT.format('discriminator (test)',
                                 *test_history['discriminator'][-1]))

            if epoch % 50 != 0:
                continue

            # save weights every epoch
            generator.save_weights(
                os.path.join(run_path, 'params_generator_epoch_{0:06d}.hdf5'.format(epoch)), True)
            discriminator.save_weights(
                os.path.join(run_path, 'params_discriminator_epoch_{0:06d}.hdf5'.format(epoch)), True)

            with open(os.path.join(run_path, 'acgan-history.pkl'), 'wb') as f:
                pickle.dump({'train': train_history, 'test': test_history}, f)

            # generate some digits to display
            num_rows = 5
            noise = np.random.uniform(-1, 1, (num_rows*6, latent_size))

            sampled_labels = np.array([
                [i] * num_rows*3 for i in range(num_classes)
            ]).reshape(-1, 1)

            # get a batch to display
            generated_images = generator.predict(
                [noise, sampled_labels], verbose=0)

            fig,axes = plt.subplots( 6, 5, figsize=[64,64])
            plt.subplots_adjust(wspace=0, hspace=0)
            for i,iax in enumerate(axes.flatten()):
                iax.imshow((generated_images[i]+1.0)/2.0, interpolation='nearest')
                iax.axis('off')

            plt.savefig(os.path.join(run_path, 'plot_epoch_{0:03d}_generated.png'.format(epoch)),
                bbox_inches='tight')

            plt.close()
