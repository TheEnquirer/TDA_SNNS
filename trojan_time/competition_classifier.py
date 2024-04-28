import os
from os.path import join
import random
import numpy as np
from rich import print, inspect
from rich.progress import track
import torch
import random
import logging
logging.basicConfig(level=logging.INFO)
import time
from typing import List, Dict, Any
from pdb import set_trace as bp




import sys
sys.path.append("./TopoTrojDetection/")
from run_crossval import run_crossval_xgb
import xgboost as xgb
from sklearn import preprocessing
from sklearn.metrics import roc_auc_score

from competition_model_data import ModelBasePaths, ModelData

def seed_everything(seed = 42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True

def load_all_models(models_dir, cache_dir):
    start = time.time()
    seed_everything()

    model_paths = sorted([x for x in os.listdir(cache_dir) if x.startswith('id')])

    # filter out all the models with empty cache dirs
    model_paths = [x for x in model_paths if len(os.listdir(join(cache_dir, x))) != 0]

    models = []
    for model_name in track(model_paths):
        base_paths = ModelBasePaths(
            model_folder_path = join(models_dir, model_name),
            cache_dir_path    = join(cache_dir, model_name)
        )

        model = ModelData(model_name,
                          base_paths,
                          initialize_from_cache=True,
                          skip_model_base=False,
                          load_fast=True
                          )

        models.append(model)

    logging.info(f"loaded {len(models)} models in {time.time() - start:.2f} seconds")
    return models

def train_xgboost(models: List[ModelData]):
    CLASSES = 5 # FIXME don't hardcode this
    n_classes = CLASSES

    TRAIN_TEST_SPLIT = 0.8

    ######################################################################
    ######################################################################

    fv_list = [x.fv for x in models]
    gt_list = [x.label for x in models]

    # PSF feature shape = N*2*m*w*h*L*C
    #   n: number of models
    #   2: logits and confidence
    #   m: number of input images
    #   w: width of the feature map
    #   h: height of the feature map
    #   L: number of stimulation levels
    #   C: number of classes

    psf_feature=torch.cat([fv_list[i]['psf_feature_pos'].unsqueeze(0) for i in range(len(fv_list))])

    # TOPO feature shape = N*12 where 12 is the total number of topological feature from dim0 and dim1
    topo_feature = torch.cat([fv_list[i]['topo_feature_pos'].unsqueeze(0) for i in range(len(fv_list))])


    topo_feature[np.where(topo_feature==np.Inf)]=1
    n, _, nEx, fnW, fnH, nStim, C = psf_feature.shape
    psf_feature_dat=psf_feature.reshape(n, 2, -1, nStim, C)
    psf_diff_max=(psf_feature_dat.max(dim=3)[0]-psf_feature_dat.min(dim=3)[0]).max(2)[0].view(len(gt_list), -1)
    psf_med_max=psf_feature_dat.median(dim=3)[0].max(2)[0].view(len(gt_list), -1)
    psf_std_max=psf_feature_dat.std(dim=3).max(2)[0].view(len(gt_list), -1)
    psf_topk_max=psf_feature_dat.topk(k=min(3, n_classes), dim=3)[0].mean(2).max(2)[0].view(len(gt_list), -1)
    psf_feature_dat=torch.cat([psf_diff_max, psf_med_max, psf_std_max, psf_topk_max], dim=1)

    dat = topo_feature.view(topo_feature.shape[0], -1)
    dat=preprocessing.scale(dat)
    gt_list=torch.tensor(gt_list)

    N = len(gt_list)
    n_train = int(TRAIN_TEST_SPLIT * N)
    ind_reshuffle = np.random.choice(list(range(N)), N, replace=False)
    train_ind = ind_reshuffle[:n_train]
    test_ind = ind_reshuffle[n_train:]

    feature_train, feature_test = dat[train_ind], dat[test_ind]
    gt_train, gt_test = gt_list[train_ind], gt_list[test_ind]

    logging.info("beginning xgboost training")
    best_model_list = run_crossval_xgb(np.array(feature_train), np.array(gt_train))


    # loop through a grid search of thresholds T and b
    for T in np.linspace(0.1, 0.9, 9):
        for b in np.linspace(0.1, 1, 10):

            # testing!
            feature = feature_test
            labels = np.array(gt_test)
            dtest = xgb.DMatrix(np.array(feature), label=labels)
            y_pred = 0
            for i in range(len(best_model_list['models'])):
                best_bst=best_model_list['models'][i]
                weight=best_model_list['weight'][i]/sum(best_model_list['weight'])
                y_pred += best_bst.predict(dtest)*weight

            y_pred = y_pred / len(best_model_list)
            # T, b=best_model_list['threshold']
            y_pred=torch.sigmoid(b*(torch.tensor(y_pred)-T)).numpy()
            acc_test = np.sum((y_pred >= 0.5)==labels)/len(y_pred)
            auc_test = roc_auc_score(labels, y_pred)
            ce_test = np.sum(-(labels * np.log(y_pred) + (1 - labels) * np.log(1 - y_pred))) / len(y_pred)

            # also test on the training set
            feature = feature_train
            labels = np.array(gt_train)
            dtest = xgb.DMatrix(np.array(feature), label=labels)
            y_pred = 0
            for i in range(len(best_model_list['models'])):
                best_bst=best_model_list['models'][i]
                weight=best_model_list['weight'][i]/sum(best_model_list['weight'])
                y_pred += best_bst.predict(dtest)*weight

            y_pred = y_pred / len(best_model_list)
            # T, b=best_model_list['threshold']
            y_pred=torch.sigmoid(b*(torch.tensor(y_pred)-T)).numpy()
            acc_train = np.sum((y_pred >= 0.5)==labels)/len(y_pred)
            auc_train = roc_auc_score(labels, y_pred)
            ce_train = np.sum(-(labels * np.log(y_pred) + (1 - labels) * np.log(1 - y_pred))) / len(y_pred)

            logging.info(f"train acc: {acc_train:.4f}, train auc: {auc_train:.4f}, train ce: {ce_train:.4f}")
            logging.info(f"test acc: {acc_test:.4f}, test auc: {auc_test:.4f}, test ce: {ce_test:.4f}")


if __name__ == "__main__":

    device = torch.device('mps')

    # TODO update this to ur device
    root = "/Users/huxley/dataset_storage/snn_tda_mats/LENET_MODELS/competition_dataset"
    models_dir = join(root, "all_models")
    cache_dir = join(root, "calculated_features_cache")

    models = load_all_models(models_dir, cache_dir)
    # models = models[:300]

    # filter for only resnets
    models = [x for x in models if x.architecture == "resnet50"]
    # models = [x for x in models if x.architecture != "resnet50"]
    print(models[0])
    # models = models[:50]

    triggered = [x for x in models if x.label == 1]
    clean = [x for x in models if x.label == 0]

    print(len(triggered), len(clean))

    min_len = min(len(triggered), len(clean))

    # balance the dataset
    triggered = triggered[:min_len]
    clean = clean[:min_len]


    models = triggered + clean
    np.random.shuffle(models)

    print(len(models))

    train_xgboost(models)



