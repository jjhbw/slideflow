import imghdr
import sys
import csv
import numpy as np
import os
import shutil

from os import listdir
from os.path import isfile, isdir, join, exists
from random import shuffle, randint
from slideflow.util import log
from glob import glob

import tensorflow as tf
import slideflow.util as sfutil

FEATURE_TYPES = (tf.int64, tf.string, tf.string)

FEATURE_DESCRIPTION_LEGACY =  {'slide':    tf.io.FixedLenFeature([], tf.string),
							   'image_raw':tf.io.FixedLenFeature([], tf.string)}

FEATURE_DESCRIPTION = {'slide':    	tf.io.FixedLenFeature([], tf.string),
					   'image_raw':	tf.io.FixedLenFeature([], tf.string),
					   'loc_x':		tf.io.FixedLenFeature([], tf.int64),
					   'loc_y':		tf.io.FixedLenFeature([], tf.int64)}

FEATURE_DESCRIPTION_MULTI =  {'slide':    tf.io.FixedLenFeature([], tf.string),
							  'input_1':  tf.io.FixedLenFeature([], tf.string),
							  'input_2':  tf.io.FixedLenFeature([], tf.string)}

class TFRecordsError(Exception):
	pass

def _float_feature(value):
	"""Returns a bytes_list from a float / double."""
	return tf.train.Feature(float_list=tf.train.FloatList(value=[value]))

def _bytes_feature(value):
	"""Returns a bytes_list from a string / byte."""
	return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

def _int64_feature(value):
	"""Returns an int64_list from a bool / enum / int / uint."""
	return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))

def _get_images_by_dir(directory):
	files = [f for f in listdir(directory) if (isfile(join(directory, f))) and
				(sfutil.path_to_ext(f) == "jpg")]
	return files

def _read_and_return_record(record, feature_description, assign_slide=None):
	features = tf.io.parse_single_example(record, feature_description)
	read_features = {f:v.numpy() for f,v in features.items()}
	if assign_slide:
		read_features['slide'] = assign_slide
	tf_example = tfrecord_example(**read_features)
	return tf_example.SerializeToString()

def _print_record(filename):
	dataset = tf.data.TFRecordDataset(filename)
	parser = get_tfrecord_parser(filename, ('slide', 'loc_x', 'loc_y'), to_numpy=True, error_if_invalid=False)

	for i, record in enumerate(dataset):
		slide, loc_x, loc_y = parser(record)
		print(f"{sfutil.header(filename)}: Record {i}: Slide: {sfutil.green(str(slide))} Loc: {(loc_x, loc_y)}")

def _decode_image(img_string, img_type, to_numpy=False, size=None, standardize=False, normalizer=None, augment=False):
	tf_decoders = {
		'png': tf.image.decode_png,
		'jpeg': tf.image.decode_jpeg,
		'jpg': tf.image.decode_jpeg
	}
	if to_numpy:
		raise NotImplementedError
	else:
		decoder = tf_decoders[img_type.lower()]
		image = decoder(img_string, channels=3)
		if normalizer: 	
			image = tf.py_function(normalizer.tf_to_rgb, [image], tf.int32)
		if standardize:
			image = tf.image.per_image_standardization(image)
		if augment:
			# Rotate randomly 0, 90, 180, 270 degrees
			image = tf.image.rot90(image, tf.random.uniform(shape=[], minval=0, maxval=4, dtype=tf.int32))
			# Random flip and rotation
			image = tf.image.random_flip_left_right(image)
			image = tf.image.random_flip_up_down(image)
		if size:
			image.set_shape([size, size, 3])
		return image

def detect_tfrecord_format(tfr):
	record = next(iter(tf.data.TFRecordDataset(tfr)))
	try:
		features = tf.io.parse_single_example(record, FEATURE_DESCRIPTION)
		feature_description = FEATURE_DESCRIPTION
	except tf.errors.InvalidArgumentError:
		try:
			features = tf.io.parse_single_example(record, FEATURE_DESCRIPTION_LEGACY)
			feature_description = FEATURE_DESCRIPTION_LEGACY
		except tf.errors.InvalidArgumentError:
			raise TFRecordsError(f"Unrecognized TFRecord format: {tfr}")
	image_type = imghdr.what('', features['image_raw'].numpy())
	return feature_description, image_type

def get_tfrecord_parser(tfrecord_path, features_to_return=None, to_numpy=False, decode_images=True, 
						standardize=False, img_size=None, normalizer=None, augment=False, error_if_invalid=True,):
	feature_description, img_type = detect_tfrecord_format(tfrecord_path)
	if features_to_return is None:
		features_to_return = list(feature_description.keys())

	def parser(record):
		features = tf.io.parse_single_example(record, feature_description)

		def process_feature(f):
			if f not in features and error_if_invalid:
				raise TFRecordsError(f"Unknown feature {f}")
			elif f not in features:
				return None
			elif f == 'image_raw' and decode_images:
				return _decode_image(features['image_raw'], img_type, to_numpy, img_size, standardize, normalizer, augment)
			elif to_numpy:
				return features[f].numpy()
			else:
				return features[f]

		if type(features_to_return) == dict:
			return {label: process_feature(f) for label, f in features_to_return.items()}
		else:
			return [process_feature(f) for f in features_to_return]

	return parser

def get_locations_from_tfrecord(filename):
	dataset = tf.data.TFRecordDataset(filename)
	loc_dict = {}
	parser = get_tfrecord_parser(filename, ('loc_x', 'loc_y'), to_numpy=True)
	for i, record in enumerate(dataset):
		loc_x, loc_y = parser(record)
		loc_dict.update({ i: (loc_x, loc_y)	})
	return loc_dict

def tfrecord_example(slide, image_string, loc_x=None, loc_y=None):
	'''Returns a Tensorflow Data example for TFRecord storage.'''
	feature = {
		'slide':     _bytes_feature(slide),
		'image_raw':_bytes_feature(image_string),
		'loc_x': _int64_feature(loc_x),
		'loc_y': _int64_feature(loc_y)
	}
	return tf.train.Example(features=tf.train.Features(feature=feature))

def multi_image_example(slide, image_dict):
	'''Returns a Tensorflow Data example for TFRecord storage with multiple images.'''
	feature = {
		'slide':	_bytes_feature(slide)
	}
	for image_label in image_dict:
		feature.update({
			image_label: _bytes_feature(image_dict[image_label])
		})
	return tf.train.Example(features=tf.train.Features(feature=feature))

def merge_split_tfrecords(source, destination):
	'''Merges TFRecords with the same name in subfolders within the given source folder,
	as may be the case when using split TFRecords for tile-level validation.'''
	tfrecords = {}
	subdirs = [d for d in listdir(source) if isdir(join(source, d))]
	for subdir in subdirs:
		tfrs = [tfr for tfr in listdir(join(source, subdir)) if isfile(join(source, subdir, tfr)) and tfr[-9:] == 'tfrecords']
		for tfr in tfrs:
			name = sfutil.path_to_name(tfr)
			if name not in tfrecords:
				tfrecords.update({name: [join(source, subdir, tfr)] })
			else:
				tfrecords[name] += [join(source, subdir, tfr)]
	for tfrecord_name in tfrecords:
		writer = tf.io.TFRecordWriter(join(destination, f'{tfrecord_name}.tfrecords'))
		datasets = []
		feature_description, img_type = detect_tfrecord_format(tfrecords.values()[0])
		for tfrecord in tfrecords[tfrecord_name]:
			n_feature_description, n_img_type = detect_tfrecord_format(tfrecord)
			if n_feature_description != feature_description or n_img_type != img_type:
				raise TFRecordsError("Mismatching tfrecord format found, unable to merge")
			dataset = tf.data.TFRecordDataset(tfrecord)
			dataset = dataset.shuffle(1000)
			dataset_iter = iter(dataset)
			datasets += [dataset_iter]
		while len(datasets):
			index = randint(0, len(datasets)-1)
			try:
				record = next(datasets[index])
			except StopIteration:
				del(datasets[index])
				continue
			writer.write(_read_and_return_record(record, feature_description, None))

def join_tfrecord(input_folder, output_file, assign_slide=None):
	'''Randomly samples from tfrecords in the input folder with shuffling,
	and combines into a single tfrecord file.'''
	writer = tf.io.TFRecordWriter(output_file)
	tfrecord_files = glob(join(input_folder, "*.tfrecords"))
	datasets = []
	if assign_slide: assign_slide = assign_slide.encode('utf-8')
	feature_description, img_type = detect_tfrecord_format(tfrecord_files[0])
	for tfrecord in tfrecord_files:
		n_feature_description, n_img_type = detect_tfrecord_format(tfrecord)
		if n_feature_description != feature_description or n_img_type != img_type:
			raise TFRecordsError("Mismatching tfrecord format found, unable to merge")
		dataset = tf.data.TFRecordDataset(tfrecord)
		dataset = dataset.shuffle(1000)
		dataset_iter = iter(dataset)
		datasets += [dataset_iter]
	while len(datasets):
		index = randint(0, len(datasets)-1)
		try:
			record = next(datasets[index])
		except StopIteration:
			del(datasets[index])
			continue
		writer.write(_read_and_return_record(record, feature_description, assign_slide))

def split_tfrecord(tfrecord_file, output_folder):
	'''Splits records from a single tfrecord file into individual tfrecord files by slide.'''
	dataset = tf.data.TFRecordDataset(tfrecord_file)
	feature_description, _ = detect_tfrecord_format(tfrecord_file)
	parser = get_tfrecord_parser(tfrecord_file, ['slide'], to_numpy=True)
	writers = {}
	for record in dataset:
		slide = parser(record)
		shortname = sfutil._shortname(slide.decode('utf-8'))

		if shortname not in writers.keys():
			tfrecord_path = join(output_folder, f"{shortname}.tfrecords")
			writer = tf.io.TFRecordWriter(tfrecord_path)
			writers.update({shortname: writer})
		else:
			writer = writers[shortname]
		writer.write(_read_and_return_record(record, feature_description))

	for slide in writers.keys():
		writers[slide].close()

def print_tfrecord(target):
	'''Prints the slide names (and locations, if present) for records in the given tfrecord file'''
	if isfile(target):
		_print_record(target)
	else:
		tfrecord_files = glob(join(target, "*.tfrecords"))
		for tfr in tfrecord_files:
			_print_record(tfr)		

def write_tfrecords_merge(input_directory, output_directory, filename):
	'''Scans a folder for subfolders, assumes subfolders are slide names. Assembles all image tiles within 
	subfolders and labels using the provided annotation_dict, assuming the subfolder is the slide name. 
	Collects all image tiles and exports into a single tfrecord file.'''
	tfrecord_path = join(output_directory, filename)
	if not exists(output_directory):
		os.makedirs(output_directory)
	image_labels = {}
	slide_dirs = [_dir for _dir in listdir(input_directory) if isdir(join(input_directory, _dir))]
	for slide_dir in slide_dirs:
		files = _get_images_by_dir(join(input_directory, slide_dir))
		for tile in files:
			image_labels.update({join(input_directory, slide_dir, tile): bytes(slide_dir, 'utf-8')})
	keys = list(image_labels.keys())
	shuffle(keys)
	with tf.io.TFRecordWriter(tfrecord_path) as writer:
		for filename in keys:
			label = image_labels[filename]
			image_string = open(filename, 'rb').read()
			tf_example = tfrecord_example(label, image_string)
			writer.write(tf_example.SerializeToString())
	log.empty(f"Wrote {len(keys)} image tiles to {sfutil.green(tfrecord_path)}", 1)
	return len(keys)

def write_tfrecords_multi(input_directory, output_directory):
	'''Scans a folder for subfolders, assumes subfolders are slide names. Assembles all image tiles within 
	subfolders and labels using the provided annotation_dict, assuming the subfolder is the slide name. 
	Collects all image tiles and exports into multiple tfrecord files, one for each slide.'''
	slide_dirs = [_dir for _dir in listdir(input_directory) if isdir(join(input_directory, _dir))]
	total_tiles = 0
	for slide_dir in slide_dirs:
		total_tiles += write_tfrecords_single(join(input_directory, slide_dir), output_directory, f'{slide_dir}.tfrecords', slide_dir)
	log.complete(f"Wrote {sfutil.bold(total_tiles)} image tiles across {sfutil.bold(len(slide_dirs))} tfrecords in {sfutil.green(output_directory)}", 1)

def write_tfrecords_single(input_directory, output_directory, filename, slide):
	'''Scans a folder for image tiles, annotates using the provided slide, exports
	into a single tfrecord file.'''
	if not exists(output_directory):
		os.makedirs(output_directory)
	tfrecord_path = join(output_directory, filename)
	image_labels = {}
	files = _get_images_by_dir(input_directory)
	for tile in files:
		image_labels.update({join(input_directory, tile): bytes(slide, 'utf-8')})
	keys = list(image_labels.keys())
	shuffle(keys)
	with tf.io.TFRecordWriter(tfrecord_path) as writer:
		for filename in keys:
			label = image_labels[filename]
			image_string = open(filename, 'rb').read()
			tf_example = tfrecord_example(label, image_string)
			writer.write(tf_example.SerializeToString())
	log.empty(f"Wrote {len(keys)} image tiles to {sfutil.green(tfrecord_path)}", 1)
	return len(keys)

def checkpoint_to_tf_model(models_dir, model_name):
	checkpoint = join(models_dir, model_name, "cp.ckpt")
	tf_model = join(models_dir, model_name, "untrained_model")
	updated_tf_model = join(models_dir, model_name, "checkpoint_model")
	model = tf.keras.models.load_model(tf_model)
	model.load_weights(checkpoint)
	try:
		model.save(updated_tf_model)
	except KeyError:
		# Not sure why this happens, something to do with the optimizer?
		pass

def split_patients_list(patients_dict, n, balance=None, randomize=True, preserved_site=False):
	'''Splits a dictionary of patients into n groups, balancing according to key "balance" if provided.'''
	patient_list = list(patients_dict.keys())
	shuffle(patient_list)

	def flatten(l):
		'''Flattens a list'''
		return [y for x in l for y in x]

	def split(a, n):
		'''Function to split a list into n components'''
		k, m = divmod(len(a), n)
		return (a[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n))

	if balance:
		# Get patient outcome labels
		patient_outcome_labels = [patients_dict[p][balance] for p in patients_dict]

		# Get unique outcomes
		unique_labels = list(set(patient_outcome_labels))
		if preserved_site:
			import pandas as pd
			import slideflow.io.preservedsite.crossfolds as cv
			
			site_list = [p[5:7] for p in patients_dict]
			df = pd.DataFrame(list(zip(patient_list, patient_outcome_labels, site_list)), columns = ['patient', 'outcome_label', 'site'])
			df = cv.generate(df, 'outcome_label', unique_labels, crossfolds = n, target_column = 'CV', patient_column = 'patient', site_column = 'site')

			log.empty(sfutil.bold("Generating Split with Preserved Site Cross Validation"))
			log.empty(sfutil.bold("Category\t" + "\t".join([str(cat) for cat in range(len(set(unique_labels)))])), 2)
			for k in range(n):
				log.empty(f"K-fold-{k}\t" + "\t".join([str(len(df[(df.CV == str(k+1)) & (df.outcome_label == o)].index)) for o in unique_labels]), 2)
			
			return [df.loc[df.CV == str(ni+1), "patient"].tolist() for ni in range(n)]
			
		else:
			# Now, split patient_list according to outcomes
			patients_split_by_outcomes = [[p for p in patient_list if patients_dict[p][balance] == uo] for uo in unique_labels]

			# Then, for each sublist, split into n components
			patients_split_by_outcomes_split_by_n = [list(split(sub_l, n)) for sub_l in patients_split_by_outcomes]

			# Print splitting as a table
			log.empty(sfutil.bold("Category\t" + "\t".join([str(cat) for cat in range(len(set(unique_labels)))])), 2)
			for k in range(n):
				log.empty(f"K-fold-{k}\t" + "\t".join([str(len(clist[k])) for clist in patients_split_by_outcomes_split_by_n]), 2)

			# Join sublists
			return [flatten([item[ni] for item in patients_split_by_outcomes_split_by_n]) for ni in range(n)]
	else:
		return list(split(patient_list, n))

def get_training_and_validation_tfrecords(dataset, validation_log, model_type, slide_labels_dict, outcome_key, validation_target, validation_strategy, 
											validation_fraction, validation_k_fold=None, k_fold_iter=None, read_only=False):
	'''From a specified subfolder within the project's main TFRecord folder, prepare a training set and validation set.
	If a validation plan has already been prepared (e.g. K-fold iterations were already determined), the previously generated plan will be used.
	Otherwise, create a new plan and log the result in the TFRecord directory so future models may use the same plan for consistency.

	Args:
		dataset:				A slideflow.datasets.Dataset object
		validation_log:			Path to .log file containing validation plans
		slide_labels_dict:		Dictionary mapping slides to labels (used for balancing outcome labels in training and validation cohorts).
									Example dictionary:
										{
											'slide1': {
												outcome_key: 'Outcome1',
												sfutil.TCGA.patient: 'patient_id'
											}
										}
		outcome_key:			Key indicating outcome variable in slide_labels_dict
		model_type:				Either 'categorical' or 'linear'
		validation_target:		Either 'per-slide' or 'per-tile'
		validation_strategy:	Either 'k-fold', 'k-fold-preserved-site', 'bootstrap', or 'fixed'.
		validation_fraction:	Float, proportion of data for validation. Not used if strategy is k-fold.
		validation_k_fold:		K, if using K-fold validation.
		k_fold_iter:			Which K-fold iteration, if using K-fold validation.

	Returns:
		Two arrays: 	an array of full paths to training tfrecords, and an array of paths to validation tfrecords.''' 

	# Prepare dataset
	tfr_folders = dataset.get_tfrecords_folders()
	subdirs = []
	for folder in tfr_folders:
		try:
			detected_subdirs = [sd for sd in os.listdir(folder) if isdir(join(folder, sd))]
		except:
			err_msg = f"Unable to find TFRecord location {sfutil.green(folder)}"
			log.error(err_msg)
			raise TFRecordsError(err_msg)
		subdirs = detected_subdirs if not subdirs else subdirs
		if detected_subdirs != subdirs:
			log.error("Unable to combine TFRecords from datasets; TFRecord subdirectory structures do not match.")
			raise TFRecordsError("Unable to combine TFRecords from datasets; TFRecord subdirectory structures do not match.")

	if k_fold_iter: 
		k_fold_index = int(k_fold_iter)-1
	k_fold = validation_k_fold
	training_tfrecords = []
	validation_tfrecords = []
	accepted_plan = None
	slide_list = list(slide_labels_dict.keys())

	# If validation is done per-tile, use pre-separated TFRecord files (validation separation done at time of TFRecord creation)
	if validation_target == 'per-tile':
		log.info(f"Attempting to load pre-separated TFRecords", 1)
		if validation_strategy == 'bootstrap':
			log.warn("Validation bootstrapping is not supported when the validation target is per-tile; using tfrecords in 'training' and 'validation' subdirectories", 1)
		if validation_strategy in ('bootstrap', 'fixed'):
			# Load tfrecords from 'validation' and 'training' subdirectories
			if ('validation' not in subdirs) or ('training' not in subdirs):
				err_msg = f"{sfutil.bold(validation_strategy)} selected as validation strategy but tfrecords are not organized as such (unable to find 'training' or 'validation' subdirectories)"
				log.error(err_msg)
				raise TFRecordsError(err_msg)
			training_tfrecords += dataset.get_tfrecords_by_subfolder("training")
			validation_tfrecords += dataset.get_tfrecords_by_subfolder("validation")
		elif validation_strategy == 'k-fold':
			if not k_fold_iter:
				log.warn("No k-fold iteration specified; assuming iteration #1", 1)
				k_fold_iter = 1
			if k_fold_iter > k_fold:
				err_msg = f"K-fold iteration supplied ({k_fold_iter}) exceeds the project K-fold setting ({k_fold})"
				log.error(err_msg, 1)
				raise TFRecordsError(err_msg)
			for k in range(k_fold):
				if k == k_fold_index:
					validation_tfrecords += dataset.get_tfrecords_by_subfolder(f'kfold-{k}')
				else:
					training_tfrecords += dataset.get_tfrecords_by_subfolder(f'kfold-{k}')
		elif validation_strategy == 'none':
			if len(subdirs):
				err_msg = f"Validation strategy set as 'none' but the TFRecord directory has been configured for validation (contains subfolders {', '.join(subdirs)})"
				log.error(err_msg, 1)
				raise TFRecordsError(err_msg)
		# Remove tfrecords not specified in slide_list
		training_tfrecords = [tfr for tfr in training_tfrecords if tfr.split('/')[-1][:-10] in slide_list]
		validation_tfrecords = [tfr for tfr in validation_tfrecords if tfr.split('/')[-1][:-10] in slide_list]

	elif validation_target == 'per-patient':
		# Assemble dictionary of patients linking to list of slides and outcome labels
		# slideflow.util.get_labels_from_annotations() ensures no duplicate outcome labels are found in a single patient
		tfrecord_dir_list = dataset.get_tfrecords()
		tfrecord_dir_list_names = [tfr.split('/')[-1][:-10] for tfr in tfrecord_dir_list]
		patients_dict = {}
		num_warned = 0
		warn_threshold = 3
		for slide in slide_list:
			patient = slide_labels_dict[slide][sfutil.TCGA.patient]
			print_func = print if num_warned < warn_threshold else None
			# Skip slides not found in directory
			if slide not in tfrecord_dir_list_names:
				log.warn(f"Slide {slide} not found in tfrecord directory, skipping", 1, print_func)
				num_warned += 1
				continue
			if patient not in patients_dict:
				patients_dict[patient] = {
					'outcome_label': slide_labels_dict[slide][outcome_key],
					'slides': [slide]
				}
			elif patients_dict[patient]['outcome_label'] != slide_labels_dict[slide][outcome_key]:
				err_msg = f"Multiple outcome labels found for patient {patient} ({patients_dict[patient]['outcome_label']}, {slide_labels_dict[slide][outcome_key]})"
				log.error(err_msg, 1)
				raise TFRecordsError(err_msg)
				
			else:
				patients_dict[patient]['slides'] += [slide]
		if num_warned >= warn_threshold:
			log.warn(f"...{num_warned} total warnings, see {sfutil.green(log.logfile)} for details", 1)
		patients = list(patients_dict.keys())
		sorted_patients = [p for p in patients]
		sorted_patients.sort()
		shuffle(patients)

		# Create and log a validation subset
		if len(subdirs):
			err_msg = f"Validation target set to 'per-patient', but the TFRecord directory has validation configured per-tile (contains subfolders {', '.join(subdirs)}"
			log.error(err_msg, 1)
			raise TFRecordsError(err_msg)
		if validation_strategy == 'none':
			log.info(f"Validation strategy set to 'none'; selecting no tfrecords for validation.", 1)
			training_slides = np.concatenate([patients_dict[patient]['slides'] for patient in patients_dict.keys()]).tolist()
			validation_slides = []
		elif validation_strategy == 'bootstrap':
			num_val = int(validation_fraction * len(patients))
			log.info(f"Using boostrap validation: selecting {sfutil.bold(num_val)} patients at random to use for validation testing", 1)
			validation_patients = patients[0:num_val]
			training_patients = patients[num_val:]
			if not len(validation_patients) or not len(training_patients):
				err_msg = "Insufficient number of patients to generate validation dataset."
				log.error(err_msg, 1)
				raise TFRecordsError(err_msg)
			validation_slides = np.concatenate([patients_dict[patient]['slides'] for patient in validation_patients]).tolist()
			training_slides = np.concatenate([patients_dict[patient]['slides'] for patient in training_patients]).tolist()
		else:
			# Try to load validation plan
			validation_plans = [] if not exists(validation_log) else sfutil.load_json(validation_log)
			for plan in validation_plans:
				# First, see if plan type is the same
				if plan['strategy'] != validation_strategy:
					continue
				# If k-fold, check that k-fold length is the same
				if (validation_strategy == 'k-fold' or validation_strategy == 'k-fold-preserved-site') and len(list(plan['tfrecords'].keys())) != k_fold:
					continue
				# Then, check if patient lists are the same
				plan_patients = list(plan['patients'].keys())
				plan_patients.sort()
				if plan_patients == sorted_patients:
					# Finally, check if outcome variables are the same
					if [patients_dict[p]['outcome_label'] for p in plan_patients] == [plan['patients'][p]['outcome_label'] for p in plan_patients]:
						log.info(f"Using {validation_strategy} validation plan detected at {sfutil.green(validation_log)}", 1)
						accepted_plan = plan
						break
			# If no plan found, create a new one
			if not accepted_plan:
				log.info(f"No suitable validation plan found; will log plan at {sfutil.green(validation_log)}", 1)
				new_plan = {
					'strategy':		validation_strategy,
					'patients':		patients_dict,
					'tfrecords':	{}
				}
				if validation_strategy == 'fixed':
					num_val = int(validation_fraction * len(patients))
					validation_patients = patients[0:num_val]
					training_patients = patients[num_val:]
					if not len(validation_patients) or not len(training_patients):
						err_msg = "Insufficient number of patients to generate validation dataset."
						log.error(err_msg, 1)
						raise TFRecordsError(err_msg)
					validation_slides = np.concatenate([patients_dict[patient]['slides'] for patient in validation_patients]).tolist()
					training_slides = np.concatenate([patients_dict[patient]['slides'] for patient in training_patients]).tolist()
					new_plan['tfrecords']['validation'] = validation_slides
					new_plan['tfrecords']['training'] = training_slides
				elif validation_strategy == 'k-fold' or validation_strategy == 'k-fold-preserved-site':
					k_fold_patients = split_patients_list(patients_dict, k_fold, balance=('outcome_label' if model_type == 'categorical' else None), randomize=True, preserved_site = validation_strategy == 'k-fold-preserved-site')
					# Verify at least one patient is in each k_fold group
					if not min([len(patients) for patients in k_fold_patients]):
						err_msg = "Insufficient number of patients to generate validation dataset."
						log.error(err_msg, 1)
						raise TFRecordsError(err_msg)
					training_patients = []
					for k in range(k_fold):
						new_plan['tfrecords'][f'k-fold-{k+1}'] = np.concatenate([patients_dict[patient]['slides'] for patient in k_fold_patients[k]]).tolist()
						if k == k_fold_index:
							validation_patients = k_fold_patients[k]
						else:
							training_patients += k_fold_patients[k]
					validation_slides = np.concatenate([patients_dict[patient]['slides'] for patient in validation_patients]).tolist()
					training_slides = np.concatenate([patients_dict[patient]['slides'] for patient in training_patients]).tolist()
				else:
					err_msg = f"Unknown validation strategy {validation_strategy} requested."
					log.error(err_msg)
					raise TFRecordsError(err_msg)
				# Write the new plan to log
				validation_plans += [new_plan]
				if not read_only:
					sfutil.write_json(validation_plans, validation_log)
			else:
				# Use existing plan
				if validation_strategy == 'fixed':
					validation_slides = accepted_plan['tfrecords']['validation']
					training_slides = accepted_plan['tfrecords']['training']
				elif validation_strategy == 'k-fold' or validation_strategy == 'k-fold-preserved-site':
					validation_slides = accepted_plan['tfrecords'][f'k-fold-{k_fold_iter}']
					training_slides = np.concatenate([accepted_plan['tfrecords'][f'k-fold-{ki+1}'] for ki in range(k_fold) if ki != k_fold_index]).tolist()
				else:
					err_msg = f"Unknown validation strategy {validation_strategy} requested."
					log.error(err_msg)
					raise TFRecordsError(err_msg)

		# Perform final integrity check to ensure no patients are in both training and validation slides
		validation_pt = list(set([slide_labels_dict[slide][sfutil.TCGA.patient] for slide in validation_slides]))
		training_pt = list(set([slide_labels_dict[slide][sfutil.TCGA.patient] for slide in training_slides]))
		if sum([pt in training_pt for pt in validation_pt]):
			err_msg = "At least one patient is in both validation and training sets."
			log.error(err_msg)
			raise TFRecordsError(err_msg)

		# Return list of tfrecords
		validation_tfrecords = [tfr for tfr in tfrecord_dir_list if sfutil.path_to_name(tfr) in validation_slides]
		training_tfrecords = [tfr for tfr in tfrecord_dir_list if sfutil.path_to_name(tfr) in training_slides]
	else:
		err_msg = f"Invalid validation strategy '{validation_target}' detected; must be either 'per-tile' or 'per-patient'."
		log.error(err_msg, 1)
		raise TFRecordsError(err_msg)
	log.info(f"Using {sfutil.bold(len(training_tfrecords))} TFRecords for training, {sfutil.bold(len(validation_tfrecords))} for validation", 1)
	return training_tfrecords, validation_tfrecords

def update_tfrecord_dir(directory, old_feature_description=FEATURE_DESCRIPTION, slide='slide', assign_slide=None, image_raw='image_raw'):
	'''Updates tfrecords in a directory from an old format to a new format.'''
	if not exists(directory):
		log.error(f"Directory {directory} does not exist; unable to update tfrecords.")
	else:
		tfrecord_files = glob(join(directory, "*.tfrecords"))
		for tfr in tfrecord_files:
			update_tfrecord(tfr, old_feature_description, slide, assign_slide, image_raw)
		return len(tfrecord_files)

def update_tfrecord(tfrecord_file, old_feature_description=FEATURE_DESCRIPTION, slide='slide', assign_slide=None, image_raw='image_raw'):
	'''Updates a single tfrecord from an old format to a new format.'''
	shutil.move(tfrecord_file, tfrecord_file+".old")
	dataset = tf.data.TFRecordDataset(tfrecord_file+".old")
	writer = tf.io.TFRecordWriter(tfrecord_file)
	feature_description, _ = detect_tfrecord_format(tfrecord_file)
	for record in dataset:
		slidename = bytes(assign_slide, 'utf-8') if assign_slide else None
		writer.write(_read_and_return_record(record, feature_description, assign_slide=slidename))
	writer.close()
	os.remove(tfrecord_file+'.old')

def transform_tfrecord(origin, target, assign_slide=None, hue_shift=None, resize=None, silent=False):
	'''Transforms images in a single tfrecord. Can perform hue shifting, resizing, or re-assigning slide label.'''
	print_func = None if silent else print
	log.empty(f"Transforming tiles in tfrecord {sfutil.green(origin)}", 1, print_func)
	log.info(f"Saving to new tfrecord at {sfutil.green(target)}", 2, print_func)
	if assign_slide:
		log.info(f"Assigning slide name {sfutil.bold(assign_slide)}", 2, print_func)
	if hue_shift:
		log.info(f"Shifting hue by {sfutil.bold(str(hue_shift))}", 2, print_func)
	if resize:
		log.info(f"Resizing records to ({resize}, {resize})", 2, print_func)

	dataset = tf.data.TFRecordDataset(origin)
	writer = tf.io.TFRecordWriter(target)
	parser = get_tfrecord_parser(origin, ('slide', 'image_raw', 'loc_x', 'loc_y'), error_if_invalid=False, to_numpy=True)

	def process_image(image_string):
		if hue_shift:
			decoded_image = tf.image.decode_png(image_string, channels=3)
			adjusted_image = tf.image.adjust_hue(decoded_image, hue_shift)
			encoded_image = tf.io.encode_jpeg(adjusted_image, quality=80)
			return encoded_image.numpy()
		elif resize:
			decoded_image = tf.image.decode_png(image_string, channels=3)
			resized_image = tf.image.resize(decoded_image, (resize, resize), method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)
			encoded_image = tf.io.encode_jpeg(resized_image, quality=80)
			return encoded_image.numpy()
		else:
			return image_string

	for record in dataset:
		slide, image_raw, loc_x, loc_y = parser(record)
		slidename = slide if not assign_slide else bytes(assign_slide, 'utf-8')
		image_processed_data = process_image(image_raw)
		tf_example = tfrecord_example(slidename, image_processed_data, loc_x, loc_y)
		writer.write(tf_example.SerializeToString())
	writer.close()

def shuffle_tfrecord(target):
	'''Shuffles records in a TFRecord, saving the original to a .old file.'''
	old_tfrecord = target+".old"
	shutil.move(target, old_tfrecord)

	dataset = tf.data.TFRecordDataset(old_tfrecord)
	writer = tf.io.TFRecordWriter(target)

	extracted_tfrecord = []
	for record in dataset:
		extracted_tfrecord += [record.numpy()]

	shuffle(extracted_tfrecord)

	for record in extracted_tfrecord:
		writer.write(record)
	
	writer.close()

def shuffle_tfrecords_by_dir(directory):
	'''For each TFRecord in a directory, shuffles records in the TFRecord, saving the original to a .old file.'''
	records = [tfr for tfr in listdir(directory) if tfr[-10:] == ".tfrecords"]
	for record in records:
		log.empty(f'Working on {record}')
		shuffle_tfrecord(join(directory, record))

def get_tfrecord_by_index(tfrecord, index, decode=True):
	'''Reads and returns an individual record from a tfrecord by index, including slide name and JPEG-processed image data.'''

	if type(index) != int:
		try:
			index = int(index)
		except:
			raise IndexError(f"index must be an integer, not {type(index)} (provided {index}).")

	dataset = tf.data.TFRecordDataset(tfrecord)
	parser = get_tfrecord_parser(tfrecord, ('slide', 'image_raw'), decode_images=decode)

	total = 0
	for i, record in enumerate(dataset):
		total += 1
		if i == index:
			return parser(record)
		else: continue

	log.error(f"Unable to find record at index {index} in {sfutil.green(tfrecord)} ({total} total records)", 1)
	return False, False

def get_tfrecords_from_model_manifest(manifest, dataset='validation'):
	slides = []
	with open(manifest, 'r') as manifest_file:
		reader = csv.reader(manifest_file)
		header = next(reader)
		dataset_index = header.index('dataset')
		slide_index = header.index('slide')
		for row in reader:
			dataset_name = row[dataset_index]
			slide_name = row[slide_index]
			if dataset_name == dataset or not dataset:
				slides += [slide_name]
	return slides

def extract_tiles(tfrecord, destination, description=FEATURE_DESCRIPTION, feature_label='image_raw'):
	'''Reads and saves images from a TFRecord to a destination folder.'''
	if not exists(destination):
		os.makedirs(destination)
	log.empty(f"Extracting tiles from tfrecord {sfutil.green(tfrecord)}", 1)
	log.info(f"Saving tiles to directory {sfutil.green(destination)}", 2)

	dataset = tf.data.TFRecordDataset(tfrecord)
	_, img_type = detect_tfrecord_format(tfrecord)
	parser = get_tfrecord_parser(tfrecord, ('slide', 'image_raw'), to_numpy=True, decode_images=False)
	for i, record in enumerate(dataset):
		slide, image_raw = parser(record)
		slidename = slide.decode('utf-8')
		dest_folder = join(destination, slidename)
		if not exists(dest_folder):
			os.makedirs(dest_folder)
		tile_filename = f"tile{i}.{img_type}"
		image_string = open(join(dest_folder, tile_filename), 'wb')
		image_string.write(image_raw)
		image_string.close()