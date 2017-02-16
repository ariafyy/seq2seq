#! /usr/bin/env python
# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Generates model predictions.
"""

import functools

import os
import yaml
import numpy as np
from matplotlib import pyplot as plt
from six import string_types

import tensorflow as tf
from tensorflow.python.platform import gfile

from seq2seq.inference import create_inference_graph, create_predictions_iter
from seq2seq.inference import unk_replace, get_unk_mapping

tf.flags.DEFINE_string("source", None, "path to source training data")
tf.flags.DEFINE_string("model_dir", None, "directory to load model from")
tf.flags.DEFINE_string("checkpoint_path", None,
                       """Full path to the checkpoint to be loaded. If None,
                       the latest checkpoint in the model dir is used.""")
tf.flags.DEFINE_string("delimiter", " ",
                       """Join predicted tokens on this delimiter.
                       Defaults to " " (space).""")
tf.flags.DEFINE_integer("batch_size", 32, "the train/dev batch size")
tf.flags.DEFINE_string("input_pipeline_def", None,
                       """Use this to overwrite the input pipeline.
                       A YAML string.""")
tf.flags.DEFINE_string("hparams", None,
                       """JSON/YAML string to override hyperparameters values.
                       For example, you can use this flag to override the
                       beam search score function.""")
tf.flags.DEFINE_boolean("unk_replace", False,
                        """UNK token replacement strategy. If None (default)
                        do no replacement. "copy" copies source words based on
                        attention score. "probs" copies words based on attention
                        scores and a dictionary of probabilities.""")
tf.flags.DEFINE_string("unk_mapping", None,
                       """Used only if "unk_replace" is set to "props". This is
                       a conditional probability file such as the one generated
                       by fast_align.
                       Refer to the documentation for more details. """)

# Attention Dumping
tf.flags.DEFINE_string("dump_attention_dir", None,
                       "Write all attention plots to this directory.")
tf.flags.DEFINE_string("dump_attention_no_plot", None,
                       "If set, does not generate attention plots.")
tf.flags.DEFINE_string("dump_beams", None,
                       "Write beam search information to this file.")


FLAGS = tf.flags.FLAGS
tf.logging.set_verbosity(tf.logging.INFO)

def get_prediction_length(predictions_dict):
  """Returns the length of the prediction based on the index
  of the first SEQUENCE_END token.
  """
  tokens_iter = enumerate(predictions_dict["predicted_tokens"])
  return next(
      ((i + 1) for i, _ in tokens_iter if _ == "SEQUENCE_END"),
      len(predictions_dict["predicted_tokens"]))

def get_scores(predictions_dict):
  """Returns the attention scores, sliced by source and target length.
  """
  prediction_len = get_prediction_length(predictions_dict)
  source_len = predictions_dict["features.source_len"]
  return predictions_dict["attention_scores"][:prediction_len, :source_len]

def create_figure(predictions_dict):
  """Creates an returns a new figure that visualizes
  attention scors for for a single model predictions.
  """

  # Find out how long the predicted sequence is
  target_words = list(predictions_dict["predicted_tokens"])

  prediction_len = get_prediction_length(predictions_dict)

  # Get source words
  source_len = predictions_dict["features.source_len"]
  source_words = predictions_dict["features.source_tokens"][:source_len]

  # Plot
  fig = plt.figure(figsize=(8, 8))
  plt.imshow(
      X=predictions_dict["attention_scores"][:prediction_len, :source_len],
      interpolation="nearest",
      cmap=plt.cm.Blues)
  plt.xticks(np.arange(source_len), source_words, rotation=45)
  plt.yticks(np.arange(prediction_len), target_words, rotation=-45)
  fig.tight_layout()

  return fig

def main(_argv):
  """Program entrypoint.
  """

  params_overrides = {}
  if isinstance(FLAGS.hparams, string_types):
    params_overrides = yaml.load(FLAGS.hparams)
    tf.logging.info("Overwriting parameters: %s", params_overrides)

  predictions, _, _ = create_inference_graph(
      model_dir=FLAGS.model_dir,
      input_file=FLAGS.source,
      batch_size=FLAGS.batch_size,
      params_overrides=params_overrides,
      input_pipeline_def=FLAGS.input_pipeline_def)

  # Filter fetched predictions to save memory
  prediction_keys = set(
      ["predicted_tokens", "features.source_len", "features.source_tokens",
       "attention_scores"])

  if FLAGS.dump_beams is not None:
    prediction_keys.update([
        "beam_search_output.predicted_ids",
        "beam_search_output.beam_parent_ids",
        "beam_search_output.scores",
        "beam_search_output.log_probs"])

  # Optional UNK token replacement
  unk_replace_fn = None
  if FLAGS.unk_replace:
    if "attention_scores" not in predictions.keys():
      raise ValueError("""To perform UNK replacement you must use a model
                       class that outputs attention scores.""")
    prediction_keys.add("attention_scores")
    mapping = None
    if FLAGS.unk_mapping is not None:
      mapping = get_unk_mapping(FLAGS.unk_mapping)
    if FLAGS.unk_replace:
      unk_replace_fn = functools.partial(unk_replace, mapping=mapping)

  predictions = {k: v for k, v in predictions.items() if k in prediction_keys}

  saver = tf.train.Saver()

  checkpoint_path = FLAGS.checkpoint_path
  if not checkpoint_path:
    checkpoint_path = tf.train.latest_checkpoint(FLAGS.model_dir)

  with tf.Session() as sess:
    # Initialize variables
    sess.run(tf.global_variables_initializer())
    sess.run(tf.local_variables_initializer())
    sess.run(tf.tables_initializer())

    # Restore checkpoint
    saver.restore(sess, checkpoint_path)
    tf.logging.info("Restored model from %s", checkpoint_path)

    # Accumulate attention scores in this array.
    # Shape: [num_examples, target_length, input_length]
    attention_scores_accum = []
    if FLAGS.dump_attention_dir is not None:
      gfile.MakeDirs(FLAGS.dump_attention_dir)

    # Accumulate beam search debug information in thse arrays
    beam_accum = {
        "predicted_ids": [],
        "beam_parent_ids": [],
        "scores": [],
        "log_probs": []
    }

    # Output predictions
    predictions_iter = create_predictions_iter(predictions, sess)
    for idx, predictions_dict in enumerate(predictions_iter):
      # Convert to unicode
      predictions_dict["predicted_tokens"] = np.char.decode(
          predictions_dict["predicted_tokens"].astype("S"), "utf-8")
      predicted_tokens = predictions_dict["predicted_tokens"]

      # If we're using beam search we take the first beam
      if np.ndim(predicted_tokens) > 1:
        predicted_tokens = predicted_tokens[:, 0]

      predictions_dict["features.source_tokens"] = np.char.decode(
          predictions_dict["features.source_tokens"].astype("S"), "utf-8")
      source_tokens = predictions_dict["features.source_tokens"]
      source_len = predictions_dict["features.source_len"]

      if unk_replace_fn is not None:
        # We slice the attention scores so that we do not
        # accidentially replace UNK with a SEQUENCE_END token
        attention_scores = predictions_dict["attention_scores"]
        attention_scores = attention_scores[:, :source_len - 1]
        predicted_tokens = unk_replace_fn(
            source_tokens=source_tokens,
            predicted_tokens=predicted_tokens,
            attention_scores=attention_scores)

      # Optionally Dump attention
      if FLAGS.dump_attention_dir is not None:
        if not FLAGS.dump_attention_no_plot:
          output_path = os.path.join(
              FLAGS.dump_attention_dir, "{:05d}.png".format(idx))
          create_figure(predictions_dict)
          plt.savefig(output_path)
          plt.close()
          tf.logging.info("Wrote %s", output_path)
        attention_scores_accum.append(get_scores(predictions_dict))

      # Optionally dump beams
      if FLAGS.dump_beams is not None:
        beam_accum["predicted_ids"] += [predictions_dict[
            "beam_search_output.predicted_ids"]]
        beam_accum["beam_parent_ids"] += [predictions_dict[
            "beam_search_output.beam_parent_ids"]]
        beam_accum["scores"] += [predictions_dict[
            "beam_search_output.scores"]]
        beam_accum["log_probs"] += [predictions_dict[
            "beam_search_output.log_probs"]]

      sent = FLAGS.delimiter.join(predicted_tokens).split("SEQUENCE_END")[0]
      # Replace special BPE tokens
      sent = sent.replace("@@ ", "")
      sent = sent.strip()

      print(sent)

    # Write attention scores
    if FLAGS.dump_attention_dir is not None:
      scores_path = os.path.join(
          FLAGS.dump_attention_dir, "attention_scores.npz")
      np.savez(scores_path, *attention_scores_accum)

    if FLAGS.dump_beams is not None:
      np.savez(FLAGS.dump_beams, **beam_accum)

if __name__ == "__main__":
  tf.app.run()
