import os
import glob
from trainyolo.client import MLModel
import yaml
import re
import numpy as np
import cv2
from trainyolo.utils.ocr import read_f1_conf

def rle_to_mask(rle):
    (h, w), counts = rle['size'], rle['counts']

    mask = np.zeros(w*h, dtype=np.uint8)

    index = 0
    zeros = True
    for count in counts:
        if not zeros:
            mask[index : index + count] = 255
        index+=count
        zeros = not zeros

    mask = np.reshape(mask, [h, w])
    
    return mask

def mask_to_polygons(mask, offset_x=0, offset_y=0, norm_x=1, norm_y=1):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_TC89_KCOS)

    polygons = []
    for countour in contours:
        epsilon = 0.001 * cv2.arcLength(countour, True)
        polygon = cv2.approxPolyDP(countour, epsilon, True)
        polygon = ((polygon.squeeze() + np.array([offset_x, offset_y])) / np.array([norm_x, norm_y])).flatten().tolist()
        polygons.append(polygon)

    return polygons

# ref: ultralytics/JSON2YOLO
def min_index(arr1, arr2):
    """Find a pair of indexes with the shortest distance. 
    Args:
        arr1: (N, 2).
        arr2: (M, 2).
    Return:
        a pair of indexes(tuple).
    """
    dis = ((arr1[:, None, :] - arr2[None, :, :]) ** 2).sum(-1)
    return np.unravel_index(np.argmin(dis, axis=None), dis.shape)

# ref: ultralytics/JSON2YOLO
def merge_polygons(polygons):
    s = []
    polygons = [np.array(i).reshape(-1, 2) for i in polygons]
    idx_list = [[] for _ in range(len(polygons))]

    # record the indexes with min distance between each segment
    for i in range(1, len(polygons)):
        idx1, idx2 = min_index(polygons[i - 1], polygons[i])
        idx_list[i - 1].append(idx1)
        idx_list[i].append(idx2)

    # use two round to connect all the segments
    for k in range(2):
        # forward connection
        if k == 0:
            for i, idx in enumerate(idx_list):
                # middle segments have two indexes
                # reverse the index of middle segments
                if len(idx) == 2 and idx[0] > idx[1]:
                    idx = idx[::-1]
                    polygons[i] = polygons[i][::-1, :]

                polygons[i] = np.roll(polygons[i], -idx[0], axis=0)
                polygons[i] = np.concatenate([polygons[i], polygons[i][:1]])
                # deal with the first segment and the last one
                if i in [0, len(idx_list) - 1]:
                    s.append(polygons[i])
                else:
                    idx = [0, idx[1] - idx[0]]
                    s.append(polygons[i][idx[0]:idx[1] + 1])

        else:
            for i in range(len(idx_list) - 1, -1, -1):
                if i not in [0, len(idx_list) - 1]:
                    idx = idx_list[i]
                    nidx = abs(idx[1] - idx[0])
                    s.append(polygons[i][nidx:])
    return s


def annotations_to_yolo_polygons(annotations, im_w, im_h):
    output = []
    for ann in annotations:
        cl, bbox, rle = ann['category_id'], ann['bbox'], ann['segmentation']
        mask = rle_to_mask(rle)
        polygons = mask_to_polygons(mask, bbox[0], bbox[1], im_w, im_h)
        if len(polygons) > 1: # multi part polygon 
            merged_polygon = merge_polygons(polygons)
            merged_polygon = np.concatenate(merged_polygon, axis=0).flatten().tolist()
            output.append([cl-1, merged_polygon])
        elif len(polygons) > 0: # sometimes empty labels are saved
            output.append([cl-1, polygons[0]])
    return output

def upload_yolov8_run(project, mode='detect', run_location=None, run=None, weights='best.pt', conf=None, iou=0.45):
    run_location = run_location or './runs'
    run_location = os.path.join(run_location, mode)

    # exp path
    if run is None:
        # get latest exp
        exp_paths = glob.glob(os.path.join(run_location, 'train*'))
        # order paths (natural ordering so a bit tricky)
        convert = lambda text: int(text) if text.isdigit() else text.lower()
        alphanum_key = lambda key: [convert(c) for c in re.split('([0-9]+)', key)]
        exp_path = sorted(exp_paths, key=alphanum_key)[-1]
    else:
        exp_path = os.path.join(run_location, run)

    # get result of best/last model
    csv_file = os.path.join(exp_path, 'results.csv')
    with open(csv_file, 'r') as f:
        lines = [[val.strip() for val in r.split(",")] for r in f.readlines()]
        headers, csv_list = lines[0], lines[1:]

        if mode == 'detect':
            results = [{'precision': float(item[4]), 'recall':float(item[5]), 'map50':float(item[6]), 'map':float(item[7])} for item in csv_list]
        else: #segment
            results = [{
                'precision': float(item[5]), 
                'recall':float(item[6]), 
                'map50':float(item[7]), 
                'map':float(item[8]),
                'precision_mask': float(item[9]), 
                'recall_mask':float(item[10]), 
                'map50_mask':float(item[11]), 
                'map_mask':float(item[12]),
                } for item in csv_list]

    if weights == 'best.pt':
        # find best result
        best_result, best_fi = None, -1
        for result in results:
            if mode == 'detect':
                fi = 0.1*result['map50'] + 0.9*result['map']
            else: #segment
                fi = 0.1*result['map50'] + 0.9*result['map'] + 0.1*result['map50_mask'] + 0.9*result['map_mask']
            if fi > best_fi:
                best_fi = fi
                best_result = result
        result = best_result
    else:
        # take last result
        result = results[-1]

    # get categories
    opt_file = os.path.join(exp_path, 'args.yaml')
    with open(opt_file, 'r') as f:
        opt = yaml.load(f, Loader=yaml.FullLoader)
    
    dataset_file = opt['data']
    with open(dataset_file, 'r') as f:
        dataset = yaml.load(f, Loader=yaml.FullLoader)
    categories = [{'id': id+1, 'name': name} for id, name in dataset['names'].items()]
    
    # read conf from f1 curve
    if conf is None:
        print('Reading best conf from F1_curve')
        if mode == 'detect':
            f1_curve = os.path.join(exp_path, 'F1_curve.png')
        else: 
            f1_curve = os.path.join(exp_path, 'BoxF1_curve.png')
        if os.path.exists(f1_curve):
            conf = read_f1_conf(project.client, f1_curve)
            if conf > 0:
                print(f'Using conf={conf}, which maximizes f1 score.')
                conf = conf
            else:
                print("Something went wrong while reading f1 curve, defaulting to conf=0.5")
                conf = 0.5
        else:
            print("Unable to find f1 curve, defaulting to conf=0.5")
            conf = 0.5

    # create model
    if project.model is None:
        model = MLModel.create(
            project.client,
            f'{project.name}',
            description='',
            type='BBOX' if mode == 'detect' else 'INSTANCE_SEGMENTATION',
            public = project.public,
            project=project.uuid
        )
    else:
        model = project.model

    print(f'adding weights: {os.path.join(exp_path, "weights", weights)} to project ...')
    model.add_version(
        os.path.join(exp_path, 'weights', weights),
        categories=categories,
        architecture='yolov8' if mode == 'detect' else 'yolov8-seg',
        params={
            'model': opt['model'],
            'imgsz': opt['imgsz'],
            'conf': conf,
            'iou': iou
        },
        metrics={
            'map50': round(result['map50'], 3),
            'map': round(result['map'], 3),
            'precision': round(result['precision'], 3),
            'recall': round(result['recall'], 3)
        } if mode == 'detect' else {
            'map50': round(result['map50'], 3),
            'map': round(result['map'], 3),
            'precision': round(result['precision'], 3),
            'recall': round(result['recall'], 3),
            'map50_mask': round(result['map50_mask'], 3),
            'map_mask': round(result['map_mask'], 3),
            'precision_mask': round(result['precision_mask'], 3),
            'recall_mask': round(result['recall_mask'], 3)
        }
    )