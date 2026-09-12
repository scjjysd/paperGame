# Copyright (c) Meta Platforms, Inc. and affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import yaml
import os
import math

def get_relative_positions(skeleton):
    positions = {}
    for part in skeleton:
        positions[part["name"]] = part["loc"]
    return positions

def calculate_offset_and_scale(human_positions, reference_part='hip'):
    # Reference part (hip) as the origin
    reference_loc = human_positions[reference_part]
    offsets = {}

    # Calculate offsets for each body part
    for part_name, loc in human_positions.items():
        if part_name == reference_part:
            offsets[part_name] = [0, 0]  # Reference point has no offset
        else:
            offsets[part_name] = [loc[0] - reference_loc[0], loc[1] - reference_loc[1]]
    
    return offsets, reference_loc

def distance(p1, p2):
    """Calculate Euclidean distance between two points."""
    return math.hypot(p2[0]-p1[0], p2[1]-p1[1])

def angle_between(a, b, c):
    """
    Calculate the angle at point b (in degrees) given three points a, b, and c.
    Angle is formed between vectors (a->b) and (c->b).
    """
    ba = [a[0]-b[0], a[1]-b[1]]
    bc = [c[0]-b[0], c[1]-b[1]]
    dot_prod = ba[0]*bc[0] + ba[1]*bc[1]
    mag_ba = math.hypot(ba[0], ba[1])
    mag_bc = math.hypot(bc[0], bc[1])
    if mag_ba == 0 or mag_bc == 0:
        return 0
    angle = math.acos(dot_prod / (mag_ba * mag_bc))
    return math.degrees(angle)

def validate_leg_group(new_skel, hip_name, knee_name, foot_name, 
                       base_ratio_threshold=2.0, base_angle_threshold=150, 
                       similarity_tolerance=0.2, reference_leg=None):
    """
    Validate a leg group by checking:
    1. The ratio between knee->foot and hip->knee distances.
    2. The knee joint angle.
    3. The vertical ordering of the joints.
    4. (Optionally) The similarity to a reference leg (for bilateral symmetry).

    For front legs (names containing 'hip_f') we apply stricter thresholds.
    
    Returns True if the leg group passes validation, False otherwise.
    """
    # Create a lookup of parts by name for ease of access
    parts = {part['name']: part for part in new_skel}
    if hip_name not in parts or knee_name not in parts or foot_name not in parts:
        return False

    hip_loc = parts[hip_name]['loc']
    knee_loc = parts[knee_name]['loc']
    foot_loc = parts[foot_name]['loc']

    d_hip_knee = distance(hip_loc, knee_loc)
    d_knee_foot = distance(knee_loc, foot_loc)
    
    # Prevent division by zero
    if d_hip_knee == 0:
        return False

    # Compute the segment ratio and knee angle
    ratio = d_knee_foot / d_hip_knee
    knee_angle = angle_between(hip_loc, knee_loc, foot_loc)

    # Apply stricter thresholds for front legs
    if "hip_f" in hip_name:
        ratio_threshold = 1.5    # Front legs must have a lower ratio, tighter constraint
        angle_threshold = 160    # And a higher knee angle (more aligned)
    else:
        ratio_threshold = base_ratio_threshold
        angle_threshold = base_angle_threshold

    # Check ratio and angle thresholds
    if ratio > ratio_threshold or knee_angle < angle_threshold:
        return False

    # Enforce vertical ordering:
    # (Assuming y increases downward; adjust if your coordinate system differs)
    if knee_loc[1] < hip_loc[1] or foot_loc[1] < knee_loc[1]:
        return False

    # If a reference leg is provided, enforce similarity in segment lengths within a tolerance.
    if reference_leg is not None:
        ref_hip, ref_knee, ref_foot = reference_leg
        ref_d_hip_knee = distance(ref_hip, ref_knee)
        ref_d_knee_foot = distance(ref_knee, ref_foot)
        if ref_d_hip_knee == 0 or ref_d_knee_foot == 0:
            return False

        # Compare each segment’s length; if either deviates by more than the tolerance, reject.
        if abs(d_hip_knee - ref_d_hip_knee) / ref_d_hip_knee > similarity_tolerance:
            return False
        if abs(d_knee_foot - ref_d_knee_foot) / ref_d_knee_foot > similarity_tolerance:
            return False

    return True


def candidate_search_for_joint(original_skel, expected_position, exclude_names, tolerance=30):
    """
    Search the original skeleton for the joint whose location is closest to the expected_position.
    Only consider joints whose names are not in exclude_names.
    Return the candidate joint (a dict) if found within the tolerance, otherwise return None.
    """
    best_candidate = None
    best_distance = float('inf')
    for part in original_skel:
        part_name = part.get('name')
        if part_name in exclude_names:
            continue
        loc = part.get('loc', [])
        if not loc:
            continue
        d = distance(loc, expected_position)
        if d < best_distance and d < tolerance:
            best_distance = d
            best_candidate = part
    return best_candidate

def validate_y_within_bounds(loc, height):
    """Ensure the y-coordinate of a joint does not exceed the image height."""
    if loc[1] >= height:
        loc[1] = height - 1  # Clamp y value to be just below the image height
    return loc

def reconfigure_leg_group(new_skel, original_skel, hip_name, knee_name, foot_name, reference_leg=None, height=None):
    """
    Reconfigure a leg group when validation fails.
    First, attempt candidate searches for the knee and foot joints using
    an expected position based on either a reference leg or fallback values.
    """
    parts = {part['name']: part for part in new_skel}
    if hip_name not in parts:
        return new_skel  # Cannot reconfigure without the hip
    hip_loc = parts[hip_name]['loc']
    
    # --- Reconfigure Knee ---
    # Calculate expected knee position:
    if reference_leg:
        # Use offset from reference leg (hip -> knee)
        ref_knee_offset = [reference_leg[1][0] - reference_leg[0][0],
                           reference_leg[1][1] - reference_leg[0][1]]
        expected_knee = [hip_loc[0] + ref_knee_offset[0], hip_loc[1] + ref_knee_offset[1]]
    else:
        # Fallback: assume a small vertical offset from the hip
        expected_knee = [hip_loc[0], hip_loc[1] + 20]
    
    expected_knee = validate_y_within_bounds(expected_knee, height)  # Validate Y
    candidate_knee = candidate_search_for_joint(original_skel, expected_knee, exclude_names={hip_name, foot_name})
    if candidate_knee:
        new_knee_loc = candidate_knee.get('loc', expected_knee)
        new_knee_loc = validate_y_within_bounds(new_knee_loc, height)  # Validate Y
        # Update or add the knee joint in new_skel:
        found = False
        for part in new_skel:
            if part['name'] == knee_name:
                part['loc'] = new_knee_loc
                found = True
                break
        if not found:
            new_skel.append({'loc': new_knee_loc, 'name': knee_name, 'parent': hip_name})
    else:
        new_knee_loc = expected_knee
        for part in new_skel:
            if part['name'] == knee_name:
                part['loc'] = new_knee_loc

    # --- Reconfigure Foot ---
    # Calculate expected foot position based on knee:
    if reference_leg:
        ref_foot_offset = [reference_leg[2][0] - reference_leg[1][0],
                           reference_leg[2][1] - reference_leg[1][1]]
        expected_foot = [new_knee_loc[0] + ref_foot_offset[0], new_knee_loc[1] + ref_foot_offset[1]]
    else:
        expected_foot = [new_knee_loc[0], new_knee_loc[1] + 20]
    
    expected_foot = validate_y_within_bounds(expected_foot, height)  # Validate Y
    candidate_foot = candidate_search_for_joint(original_skel, expected_foot, exclude_names={hip_name, knee_name})
    if candidate_foot:
        new_foot_loc = candidate_foot.get('loc', expected_foot)
        new_foot_loc = validate_y_within_bounds(new_foot_loc, height)  # Validate Y
        # Update or add the foot joint in new_skel:
        updated = False
        for part in new_skel:
            if part['name'] == foot_name:
                part['loc'] = new_foot_loc
                updated = True
                break
        if not updated:
            new_skel.append({'loc': new_foot_loc, 'name': foot_name, 'parent': knee_name})
    else:
        for part in new_skel:
            if part['name'] == foot_name:
                part['loc'] = expected_foot
                break

    print(f"Reconfigured leg group: {hip_name}, {knee_name}, {foot_name} using candidate search.")
    return new_skel

def convert_human_to_animal(input_data):
    # Extract the height and skeleton data
    height = input_data.get('height', 0)
    skeleton = input_data.get('skeleton', [])
    
    # Save a copy of the original skeleton for candidate search
    original_skel = skeleton.copy()
    
    # Get positions for mapping
    positions = get_relative_positions(skeleton)
    
    # Create a mapping from human to animal parts
    part_mapping = {
        'hip': 'hip',
        'torso': 'torso',
        'neck': 'neck',
        'right_hip': 'right_hip_b',
        'right_knee': 'right_knee_b',
        'right_foot': 'right_foot_b',
        'left_hip': 'left_hip_b',
        'left_knee': 'left_knee_b',
        'left_foot': 'left_foot_b',
        'right_shoulder': 'right_hip_f',
        'right_elbow': 'right_knee_f',
        'right_hand': 'right_foot_f',
        'left_shoulder': 'left_hip_f',
        'left_elbow': 'left_knee_f',
        'left_hand': 'left_foot_f'
    }
    
    # Also map parents
    parent_mapping = {
        'right_hip_f': 'root',
        'left_hip_f': 'root',
        'right_hip_b': 'root',
        'left_hip_b': 'root'
    }
    
    # Include only the parts we want in animal skeleton
    included_parts = set(part_mapping.values())
    included_parts.add('root')  # Add root explicitly
    
    # Placeholder for the new skeleton structure
    new_skeleton = []
    
    # Calculate a proper neck position for quadruped anatomy
    torso_pos = positions.get('torso', [0, 0])
    hip_pos = positions.get('hip', [0, 0])
    
    # Direction vector from hip to torso (forward direction)
    forward_vector = [torso_pos[0] - hip_pos[0], torso_pos[1] - hip_pos[1]]
    neck_pos = [
        torso_pos[0] - forward_vector[0] * 0.3,
        torso_pos[1] - forward_vector[1] * 0.3
    ]
    
    # Process each joint to update their parent-child relations and names
    for part in skeleton:
        name = part.get('name', '')
        # Skip parts that aren't in our mapping
        if name not in part_mapping:
            continue
            
        new_name = part_mapping[name]
        if new_name not in included_parts:
            continue
            
        loc = part.get('loc', [])
        parent = part.get('parent', None)
        
        # Adjust parent if needed
        if new_name in parent_mapping:
            new_parent = parent_mapping[new_name]
        elif parent in part_mapping:
            new_parent = part_mapping[parent]
        else:
            new_parent = parent
        
        # Special case for neck
        if new_name == 'neck':
            loc = neck_pos
        
        loc_copy = list(loc) if loc else []
        new_part = {
            'loc': loc_copy,
            'name': new_name,
            'parent': new_parent
        }
        new_skeleton.append(new_part)
    
    # Add root explicitly if missing
    if 'root' not in [part.get('name') for part in new_skeleton]:
        hip_loc = positions.get('hip', [0, 0])
        new_skeleton.insert(0, {
            'loc': list(hip_loc),
            'name': 'root',
            'parent': None
        })
    
    # --- Validation & Candidate Search Section ---
    # Define leg groups as tuples of (hip, knee, foot)
    leg_groups = [
        ("right_hip_f", "right_knee_f", "right_foot_f"),
        ("left_hip_f", "left_knee_f", "left_foot_f"),
        ("right_hip_b", "right_knee_b", "right_foot_b"),
        ("left_hip_b", "left_knee_b", "left_foot_b")
    ]
    
    # Gather valid leg groups for potential reference use.
    valid_leg_refs = {}
    for group in leg_groups:
        hip_name, knee_name, foot_name = group
        if validate_leg_group(new_skeleton, hip_name, knee_name, foot_name):
            valid_leg_refs[hip_name] = (
                next(part['loc'] for part in new_skeleton if part['name'] == hip_name),
                next(part['loc'] for part in new_skeleton if part['name'] == knee_name),
                next(part['loc'] for part in new_skeleton if part['name'] == foot_name)
            )
    
    # For each leg group that fails validation, attempt to reconfigure it using candidate search.
    for group in leg_groups:
        hip_name, knee_name, foot_name = group
        if not validate_leg_group(new_skeleton, hip_name, knee_name, foot_name):
            reference_leg = None
            if "hip_f" in hip_name:
                other_side = "left_hip_f" if hip_name == "right_hip_f" else "right_hip_f"
                if other_side in valid_leg_refs:
                    reference_leg = valid_leg_refs[other_side]
            elif "hip_b" in hip_name:
                other_side = "left_hip_b" if hip_name == "right_hip_b" else "right_hip_b"
                if other_side in valid_leg_refs:
                    reference_leg = valid_leg_refs[other_side]
            
            new_skeleton = reconfigure_leg_group(new_skeleton, original_skel, hip_name, knee_name, foot_name, reference_leg, height)
    
    return {
        'height': height,
        'skeleton': new_skeleton,
        'width': input_data.get('width', 0)
    }

def write_animal_config(human_yaml_path):
    # Read human config
    with open(human_yaml_path, 'r') as file:
        human_config = yaml.safe_load(file)

    # Convert to animal skeleton
    animal_config = convert_human_to_animal(human_config)

    # Define a custom representer to force YAML to not use aliases
    def represent_dict(dumper, data):
        return dumper.represent_mapping(yaml.resolver.Resolver.DEFAULT_MAPPING_TAG, data.items())
    yaml.add_representer(dict, represent_dict)

    # Write the updated configuration to a new YAML file.
    output_dir = os.path.dirname(human_yaml_path)
    output_file = os.path.join(output_dir, 'animal_config.yaml')
    with open(output_file, 'w') as file:
        yaml.dump(animal_config, file, default_flow_style=False)
    print(f"Animal config has been written to {output_file}")

# Example usage:
if __name__ == "__main__":
    import sys
    # Expect human YAML file path as first argument; output directory is optional.
    if len(sys.argv) < 2:
        print("Usage: python human_to_animal.py <input_yaml_path> [output_dir]")
    else:
        human_yaml_path = sys.argv[1]
        write_animal_config(human_yaml_path)
