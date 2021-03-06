#!/usr/bin/env python

# Import modules
import matplotlib.pyplot as plt
import numpy as np
import sklearn
from sklearn.preprocessing import LabelEncoder
import pickle
from sensor_stick.srv import GetNormals
from sensor_stick.features import compute_color_histograms
from sensor_stick.features import compute_normal_histograms
from visualization_msgs.msg import Marker
from sensor_stick.marker_tools import *
from sensor_stick.msg import DetectedObjectsArray
from sensor_stick.msg import DetectedObject
from sensor_stick.pcl_helper import *

import rospy
import tf
from geometry_msgs.msg import Pose
from std_msgs.msg import Float64
from std_msgs.msg import Int32
from std_msgs.msg import String
from pr2_robot.srv import *
from rospy_message_converter import message_converter
from sensor_msgs.msg import JointState
import yaml
import argparse

from pcl_helper import ros_to_pcl2

# PR2 movement support routines

# ... close enough
def at_goal(pos, goal):
    tolerance = .05
    result = abs(pos - goal) <= abs(tolerance)
    return result

# the wait arguments determines whether or not this is a blocking call
def turn_pr2(pos, wait=True):
    time_elapsed = rospy.Time.now()
    pub_body.publish(pos)
    loc = 0.0

    joint_state = rospy.wait_for_message('/pr2/joint_states', JointState)
    loc = joint_state.position[19]	# the world link
        
    while wait:
        joint_state = rospy.wait_for_message('/pr2/joint_states', JointState)
        loc = joint_state.position[19]
 	if at_goal(loc, pos):
 	   #print "turn_pr2: Request: %f Joint %s=%f" % (pos, joint_state.name[19], joint_state.position[19])
           time_elapsed = joint_state.header.stamp - time_elapsed
           break

    return loc

def is_challenge():
	return test_scene_num.data == 4

# Helper function to get surface normals
def get_normals(cloud):
    get_normals_prox = rospy.ServiceProxy('/feature_extractor/get_normals', GetNormals)
    return get_normals_prox(cloud).cluster

# Helper function to create a yaml friendly dictionary from ROS messages
def make_yaml_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose):
    yaml_dict = {}
    yaml_dict["test_scene_num"] = test_scene_num.data
    yaml_dict["arm_name"]  = arm_name.data
    yaml_dict["object_name"] = object_name.data
    yaml_dict["pick_pose"] = message_converter.convert_ros_message_to_dictionary(pick_pose)
    yaml_dict["place_pose"] = message_converter.convert_ros_message_to_dictionary(place_pose)
    return yaml_dict

# Helper function to output to yaml file
def send_to_yaml(yaml_filename, dict_list):
    data_dict = {"object_list": dict_list}
    with open(yaml_filename, 'w') as outfile:
        yaml.dump(data_dict, outfile, default_flow_style=False)

# Callback function for your Point Cloud Subscriber
def pcl_callback(pcl_msg):

	global detected_objects
	global detected_objects_labels
	global collision_map
	global right_done
	global left_done
	global center_done
	global turn

	# Exercise-2 TODOs:

	# Convert ROS msg to PCL data

	cloud = ros_to_pcl(pcl_msg)

	# Create a VoxelGrid filter object for our input point cloud
	vox = cloud.make_voxel_grid_filter()

	# Choose a voxel (also known as leaf) size
	LEAF_SIZE = 0.01

	# Set the voxel (or leaf) size
	vox.set_leaf_size(LEAF_SIZE, LEAF_SIZE, LEAF_SIZE)

	# Call the filter function to obtain the resultant downsampled point cloud
	cloud_filtered = vox.filter()
	
	# Much like the previous filters, we start by creating a filter object: 
	outlier_filter = cloud_filtered.make_statistical_outlier_filter()

	# Set the number of neighboring points to analyze for any given point
	outlier_filter.set_mean_k(50)

	# Set threshold scale factor
	x = 0.05

	# Any point with a mean distance larger than global (mean distance+x*std_dev) will be considered outlier
	outlier_filter.set_std_dev_mul_thresh(x)

	# Finally call the filter function for magic
	cloud_filtered = outlier_filter.filter()
	
	# Create a PassThrough filter object for the first 3 worlds
	if is_challenge() and not at_goal(turn, 0.0):
		# Filter the y axis to remove outlying remove table and stand
		passthrough = cloud_filtered.make_passthrough_filter()
		passthrough.set_filter_field_name('y')
		axis_min = -0.2
		axis_max = 0.2
		passthrough.set_filter_limits(axis_min, axis_max)
		x_indices = passthrough.filter()
		cloud_filtered = passthrough.filter()
				
		# Filter out the top table. 
		passthrough.set_filter_field_name('z')
		axis_max = 2.0
		axis_min = 0.65
		passthrough.set_filter_limits (axis_min, axis_max)
		top_cloud = passthrough.filter()
		
		# Filter out the bottom table. 
		axis_max = 0.58
		axis_min = 0.12
		passthrough.set_filter_limits (axis_min, axis_max)
		bottom_cloud = passthrough.filter()
		cloud_filtered = ros_to_pcl2(pcl_to_ros(bottom_cloud), pcl_to_ros(top_cloud))
	else:	# filter both y and z axes
		passthrough = cloud_filtered.make_passthrough_filter()
		# Assign axis and range to the passthrough filter object.
		# first filter in y axis to remove bins
		passthrough.set_filter_field_name('y')
		axis_min = -0.4
		axis_max = 0.4
		passthrough.set_filter_limits(axis_min, axis_max)
		x_indices = passthrough.filter()
		cloud_filtered = passthrough.filter()

		# now filter in z axis to remove table and stand
		passthrough = cloud_filtered.make_passthrough_filter()
		passthrough.set_filter_field_name('z')
		axis_min = 0.6
		axis_max = 2.0
		passthrough.set_filter_limits (axis_min, axis_max)
		# Finally use the filter function to obtain the resultant point cloud. 
		cloud_filtered = passthrough.filter()

	
	# RANSAC plane segmentation
	# Create the segmentation object
	seg = cloud_filtered.make_segmenter()

	# Set the model you wish to fit 
	seg.set_model_type(pcl.SACMODEL_PLANE)
	seg.set_method_type(pcl.SAC_RANSAC)

	# Max distance for a point to be considered fitting the model
	# Experiment with different values for max_distance 
	# for segmenting the table
	max_distance = 0.01
	seg.set_distance_threshold(max_distance)

	# Call the segment function to obtain set of inlier indices and model coefficients
	inliers, coefficients = seg.segment()

	# Extract inliers

	cloud_table = cloud_filtered.extract(inliers, negative=False)

	# Extract outliers

	cloud_objects = cloud_filtered.extract(inliers, negative=True)

	# Euclidean Clustering

	white_cloud = XYZRGB_to_XYZ(cloud_objects)
	tree = white_cloud.make_kdtree()

	# Create Cluster-Mask Point Cloud to visualize each cluster separately
	# Create a cluster extraction object
	ec = white_cloud.make_EuclideanClusterExtraction()
	# Set tolerances for distance threshold 
	# as well as minimum and maximum cluster size (in points)
	ec.set_ClusterTolerance(0.025)
	ec.set_MinClusterSize(10)
	ec.set_MaxClusterSize(2000)
	# Search the k-d tree for clusters
	ec.set_SearchMethod(tree)
	# Extract indices for each of the discovered clusters
	cluster_indices = ec.Extract()

	#Assign a color corresponding to each segmented object in scene
	cluster_color = get_color_list(len(cluster_indices))

	color_cluster_point_list = []

	for j, indices in enumerate(cluster_indices):
	    for i, index in enumerate(indices):
		color_cluster_point_list.append([white_cloud[index][0],
		                                white_cloud[index][1],
		                                white_cloud[index][2],
		                                 rgb_to_float(cluster_color[j])])

	#Create new cloud containing all clusters, each with unique color
	cluster_cloud = pcl.PointCloud_PointXYZRGB()
	cluster_cloud.from_list(color_cluster_point_list)

	# Convert PCL data to ROS messages

	collision_map = cloud_table
	ros_cloud_table = pcl_to_ros(collision_map)
	ros_cloud_objects = pcl_to_ros(cloud_objects)
	ros_cluster_cloud = pcl_to_ros(cluster_cloud)

	# Publish ROS messages

	pcl_objects_pub.publish(ros_cloud_objects)
	pcl_table_pub.publish(ros_cloud_table)
	pcl_cluster_pub.publish(ros_cluster_cloud)
	#pcl_collision_pub.publish(ros_cloud_table)

	# Exercise-3: 

	plotting = False
	trimming = False
	far_right = -np.pi/2.0 - 0.3
	far_left = np.pi/2.0 + 0.3

	# Classify the clusters! (loop through each detected cluster one at a time)
	if at_goal(turn, far_left) or at_goal(turn, far_right) or at_goal(turn, 0.0):	# make sure PR2 is stopped
	    for index, pts_list in enumerate(cluster_indices):
		# Grab the points for the cluster
		pcl_cluster = cloud_objects.extract(pts_list)

		# Convert the cluster from pcl to ROS using helper function
		ros_cluster = pcl_to_ros(pcl_cluster)

		# Extract histogram features
		chists = compute_color_histograms(ros_cluster, using_hsv=True,nbins=64)
		normals = get_normals(ros_cluster)
		nhists = compute_normal_histograms(normals,nbins=40)
		feature = np.concatenate((chists, nhists))

		# Make the prediction, retrieve the label for the result
		# and add it to detected_objects_labels list
		prediction = clf.predict(scaler.transform(feature.reshape(1,-1)))
		label = encoder.inverse_transform(prediction)[0]
		# plot histograms for debugging purposes
		if plotting == True:
			if feature is not None:
			    fig = plt.figure(figsize=(12,6))
			    plt.plot(chists)
			    plt.plot(nhists)
			    plt.title('HSV and Normals Feature Vectors for %s' % label, fontsize=30)
			    plt.tick_params(axis='both', which='major', labelsize=20)
			    fig.tight_layout()
			    plt.savefig('%s%d_orec_features.png' % (label, len(detected_objects_labels)))
			    plt.show()
			else:
			    print('Your function is returning None...')
		
		# Publish a label into RViz
		label_pos = list(white_cloud[pts_list[0]])
		label_pos[2] += .2
		object_markers_pub.publish(make_label(label,label_pos, index))

		# Add the detected object to the list of detected objects.
		do = DetectedObject()
		do.label = label
		do.cloud = ros_cluster
		# don't add objects already detected
		if trimming == True:
			found = False
			for o in detected_objects:
				if o.label == label:
					o.cloud = ros_cluster
					found = True
			if found == False:
				detected_objects_labels.append(label)
				detected_objects.append(do)
		else:
			detected_objects.append(do)
			detected_objects_labels.append(label)

		rospy.loginfo('Detected {} objects: {}'.format(len(detected_objects_labels), detected_objects_labels))

		# Publish the list of detected objects
		detected_objects_pub.publish(detected_objects)

    	# Rotate PR2 in place to capture side tables for the collision map
	# Note that turn_pr2 is a non-blocking call so the request is made
	# and then repeatedly checked in each callback against the goal
	# when the goal is reached, the next scan is initiated, until
	# return to center is achieved
	if with_collision_map == True and center_done == False:
		print "Sending command to scan for obstacles..."
		if right_done == False:
			turn = turn_pr2(far_right, False)	# right
			print "Turning right, at %f radians now." % turn
			if at_goal(turn, far_right):
				right_done = True
		elif left_done == False:
        		turn = turn_pr2(far_left, False)	# left
 			print "Turning left, at %f radians now." % turn
			if at_goal(turn, far_left):
				left_done = True
	       	elif center_done == False:
 			turn = turn_pr2(0.0,False)		# back home
			print "Centering at %f radians now." % turn
			if at_goal(turn, 0.0):
				center_done = True

	# Add some logic to determine whether or not the object detections are robust
	# before calling pr2_mover()
	try:
		if len(detected_objects) > 0:
			if pipeline_only or with_collision_map:
				print "Skipping movements"
			else:
				pr2_mover(detected_objects)

	except rospy.ROSInterruptException:
		pass

	detected_objects = []					# start fresh

	return

# function to load parameters and request PickPlace service
def pr2_mover(detected_objects_list):

    global collision_map

    # Initialize variables

    labels = []
    centroids = [] # a list of tuples (x, y, z)
    dict_list = []
    object_name = String()
    object_group = String()
    arm_name = String()
    pick_pose = Pose()
    place_pose = Pose()

    # get parameters
    # the objects that we must pick up
    object_list_param = rospy.get_param('/object_list')

    request_count = 0
    success_count = 0
    for i in range(len(object_list_param)):
	request_count += 1
    	# Parse parameters into individual variables
	object_name.data = object_list_param[i]['name']
	object_group.data = object_list_param[i]['group']
	print "Request to pick up %s in group %s" % (object_name.data, object_group.data)
	
	# First publish all object not in the request list as collision objects
	if with_object_collision_map == True:
		ros_composite_map = pcl_to_ros(collision_map)
	    	for the_object in detected_objects_list:
		    if the_object.label != object_name.data:
			pcl_composite_map = ros_to_pcl2(ros_composite_map, the_object.cloud)	# append the new object to the collision map
			ros_composite_map = pcl_to_ros(pcl_composite_map)
			print "Publishing %s as a collision object of %s" % (the_object.label, object_name.data)

		pcl_collision_pub.publish(ros_composite_map)

   	# Loop through the pick list and look for the requested object
    	for the_object in detected_objects_list:
	    match_count = 0
	    if the_object.label == object_name.data:
		match_count += 1

		# Get the PointCloud for a given object and obtain it's centroid
		labels.append(the_object.label)
		points_arr = ros_to_pcl(the_object.cloud).to_array()
		centroid = np.mean(points_arr, axis=0)[:3]
		top = np.max(points_arr, axis=0)[:3]
		bot = np.min(points_arr, axis=0)[:3]
		height = np.asscalar(top[2])-np.asscalar(bot[2])
		centroid = [np.asscalar(centroid[0]),np.asscalar(centroid[1]),np.asscalar(centroid[2])]
		centroids.append(centroid)
		print "Found %s at: %f %f %f height=%f" % (object_name.data, centroid[0], centroid[1], centroid[2], height)

        	# Assign the arm to be used for pick_place
		if object_group.data == 'green':
		    arm_name.data = 'right'
		    place_pose.position.x = -0.1-float(success_count)*0.2	# move back a little bit for each object
		    place_pose.position.y = -0.71				# so as not to stack...
		    place_pose.position.z = 0.605
		else:
		    arm_name.data = 'left'
		    place_pose.position.x = -0.1-float(success_count)*0.2
		    place_pose.position.y = 0.71
		    place_pose.position.z = 0.605

		pick_pose.position.x = centroid[0]
		pick_pose.position.y = centroid[1]
		pick_pose.position.z = centroid[2]-height

		print "Scene %d, picking up object %s that I found, with my %s arm, and placing it in the %s bin." % (test_scene_num.data, object_name.data, arm_name.data, object_group.data)

        	# Create a list of dictionaries (made with make_yaml_dict()) for later output to yaml format
		yaml_dict = make_yaml_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose)
		dict_list.append(yaml_dict)
		

	if yaml_only == False:

		# Wait for 'pick_place_routine' service to come up
		rospy.wait_for_service('pick_place_routine')

		try:
			pick_place_routine = rospy.ServiceProxy('pick_place_routine', PickPlace)

			# Insert message variables to be sent as a service request
			resp = pick_place_routine(test_scene_num, object_name, arm_name, pick_pose, place_pose)

			print "Response to pick_place_routine service request: ", resp.success
			if resp.success == True:
				success_count += 1

		except rospy.ServiceException, e:
			print "Service call failed: %s" % e

    	# Output your request parameters into output yaml file
    	send_to_yaml('output_%d.yaml' % test_scene_num.data, dict_list)

	print "Scene %d: %d of %d objects moved to bin." % (test_scene_num.data, success_count, request_count)

    return

if __name__ == '__main__':

	pipeline_only = False			# for testing, just do the recoginition pipeline and skip PR2 movement
	with_collision_map = False		# calculate the collision map for challenge.world
	with_object_collision_map = False	# calculate the object collision map
	yaml_only = False			# true if only yaml output is desired, and no robot motion

	turn = 0.0				# current turn angle - 0.0 = front facing

	left_done = False
	right_done = False
	center_done = False

	# Parse arguments

	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", help="increase output verbosity",
                    action="store_true")
	parser.add_argument("-p", "--pipeline_only", help="pipeline only, no PR2 movement",
                    action="store_true")
	parser.add_argument("-y", "--yaml_only", help="YAML only, no PR2 movement",
                    action="store_true")
	parser.add_argument("-o", "--with_object_collision_map", help="add object collision map",
                    action="store_true")
	parser.add_argument("-m", "--with_collision_map", help="add table collision map",
                    action="store_true")

	args = parser.parse_args()

	if args.verbose:
    		print "verbosity turned on"

	if args.pipeline_only:
    		print "pipeline_only turned on"
		pipeline_only = True

	if args.yaml_only:
    		print "yaml_only turned on"
		yaml_only = True

	if args.with_collision_map:
    		print "with_collision_map turned on"
		with_collision_map = True

	if args.with_object_collision_map:
    		print "with_object_collision_map turned on"
		with_object_collision_map = True

	test_scene_num = Int32()

	# ROS node initialization

	rospy.init_node('clustering', anonymous=True)

	detected_objects_labels = []
	detected_objects = []

	collision_map = pcl.PointCloud()

	# get scene number from modified launch file
	test_scene_num.data = rospy.get_param('/test_scene_num')
	print "Test scene %d" % test_scene_num.data


	# Create Subscribers

	pcl_sub = rospy.Subscriber("/pr2/world/points", PointCloud2, pcl_callback, queue_size=12)

	# Create Publishers

	pcl_objects_pub = rospy.Publisher("/pcl_objects", PointCloud2, queue_size=12)
	pcl_table_pub = rospy.Publisher("/pcl_table", PointCloud2, queue_size=12)
	pcl_cluster_pub = rospy.Publisher("/pcl_cluster", PointCloud2, queue_size=12)
	pcl_collision_pub = rospy.Publisher("/pr2/3d_map/points", PointCloud2, queue_size=12)
	
	pub_body = rospy.Publisher('/pr2/world_joint_controller/command', Float64, queue_size=12)

	# Create object_markers_pub and detected_objects_pub
	# Publish to "/object_markers" and "/detected_objects", respectively
	object_markers_pub = rospy.Publisher("/object_markers", Marker, queue_size=12)
	detected_objects_pub = rospy.Publisher("/detected_objects", DetectedObjectsArray, queue_size=12)

	# Load Model From disk
	model = pickle.load(open('model.sav', 'rb'))
	clf = model['classifier']
	encoder = LabelEncoder()
	encoder.classes_ = model['classes']
	scaler = model['scaler']

	# Initialize color_list
	get_color_list.color_list = []

	# Spin while node is not shutdown

	while not rospy.is_shutdown():
		rospy.spin()

