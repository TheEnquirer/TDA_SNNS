import os
from os.path import join
import random
import numpy as np
from rich import print, inspect
from rich.progress import track
from tqdm import tqdm
import torch
import random
import logging
# logging.basicConfig(level=logging.INFO)
# logging.getLogger('hyperopt').setLevel(logging.WARNING)
from colorlog import ColoredFormatter
import time
from typing import List, Dict, Any
from pdb import set_trace as bp
import sys
sys.path.append("./TopoTrojDetection/")
import xgboost as xgb
from sklearn import preprocessing
from sklearn.metrics import roc_auc_score
from matplotlib import pyplot as plt
import multiprocessing
import ripser
import persim
from sklearn.metrics.pairwise import pairwise_distances
from scipy import sparse
import rustworkx as rx
import igraph as ig
import networkx as nx

from topological_feature_extractor import getGreedyPerm, getApproxSparseDM


from competition_model_data import ModelBasePaths, ModelData
from classifier_bin import xgb_classifier
from competition_classifier import load_all_models, featurize

def local_featurize(models: List[ModelData]):

    CLASSES = 5 # FIXME don't hardcode this
    n_classes = CLASSES
    fv_list = [x.fv for x in models]
    gt_list = [x.label for x in models]

    psf_feature=torch.cat([fv_list[i]['psf_feature_pos'].unsqueeze(0) for i in range(len(fv_list))])
    topo_feature = torch.cat([fv_list[i]['topo_feature_pos'].unsqueeze(0) for i in range(len(fv_list))])

    topo_feature[np.where(topo_feature==np.Inf)]=1
    n, _, nEx, fnW, fnH, nStim, C = psf_feature.shape
    psf_feature_dat=psf_feature.reshape(n, 2, -1, nStim, C)
    psf_diff_max=(psf_feature_dat.max(dim=3)[0]-psf_feature_dat.min(dim=3)[0]).max(2)[0].view(len(gt_list), -1)
    psf_med_max=psf_feature_dat.median(dim=3)[0].max(2)[0].view(len(gt_list), -1)
    psf_std_max=psf_feature_dat.std(dim=3).max(2)[0].view(len(gt_list), -1)
    psf_topk_max=psf_feature_dat.topk(k=min(3, n_classes), dim=3)[0].mean(2).max(2)[0].view(len(gt_list), -1)
    psf_feature_dat=torch.cat([psf_diff_max, psf_med_max, psf_std_max, psf_topk_max], dim=1)

    # dat=torch.cat([psf_feature_dat, topo_feature.view(topo_feature.shape[0], -1)], dim=1)

    dat=psf_feature_dat

    # dat = topo_feature.view(topo_feature.shape[0], -1)

    dat=preprocessing.scale(dat)
    gt_list=torch.tensor(gt_list)

    return {
        "features": np.array(dat),
        "labels": np.array(gt_list)
    }


device = torch.device('mps')

# TODO update this to ur device
root = "/Users/huxley/dataset_storage/snn_tda_mats/LENET_MODELS/competition_dataset"
# root = "/home/jerryhan/Documents/data"
models_dir = join(root, "all_models")
cache_dir = join(root, "calculated_features_cache")

models = load_all_models(models_dir, cache_dir)

# filter for only resnets
models = [x for x in models if x.architecture == "resnet50"]
# models = [x for x in models if x.architecture != "resnet50"]

triggered = [x for x in models if x.label == 1]
clean = [x for x in models if x.label == 0]

print(len(triggered), len(clean))
min_len = min(len(triggered), len(clean))

triggered = triggered[:min_len]
clean = clean[:min_len]

models = triggered + clean
np.random.shuffle(models)
print(len(models), "\n\n\n")

ft = local_featurize(models)


# get all the clean models from the output of local_featurize
clean_fts = np.array([ft["features"][i] for i in range(len(models)) if ft["labels"][i] == 0])
dirty_fts = np.array([ft["features"][i] for i in range(len(models)) if ft["labels"][i] == 1])


def graph_assortativity(): # GOOD

    clean_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in clean]
    dirty_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in triggered]

    # make all the nans 0
    for mat in [*clean_adj_mats, *dirty_adj_mats]:
        mat[np.isnan(mat)] = 0

    print("starting to graph convert")
    clean_graphs = [ig.Graph.Weighted_Adjacency(x.tolist(), mode="undirected") for x in clean_adj_mats]
    dirty_graphs = [ig.Graph.Weighted_Adjacency(x.tolist(), mode="undirected") for x in dirty_adj_mats]
    print("finished graph convert")

    sample = len(clean_graphs)

    NUM_NEURONS = 1467
    # model_shape = [300, 300, 300, 300, 300]
    # bin
    NUM_BINS = 2
    model_shape = [(NUM_NEURONS + NUM_BINS + 1)//NUM_BINS for _ in range(NUM_BINS)]

    c_f = []
    for x in tqdm(clean_graphs[:sample]):
        # label each node by what layer in resnet50 it corresponds to
        # assume that the nodes are ordered by layer,
        # and label each node with the layer it corresponds to, by looking at the shape of the model
        types1 = []
        for node in range(x.vcount()):
            for i, layer in enumerate(model_shape):
                if node < sum(model_shape[:i+1]):
                    types1.append(i)
                    break

        c_f.append(x.assortativity(types1, directed=False))

    d_f = []
    for x in tqdm(dirty_graphs[:sample]):
        types1 = []
        for node in range(x.vcount()):
            for i, layer in enumerate(model_shape):
                if node < sum(model_shape[:i+1]):
                    types1.append(i)
                    break

        d_f.append(x.assortativity(types1, directed=False))

    plt.hist(c_f, alpha=0.5, label='clean', color='green')
    plt.hist(d_f, alpha=0.5, label='dirty', color='red')
    plt.legend(loc='upper right')
    plt.show()

# graph_assortativity()

def graph_assortativity_degree(): # GOOD

    clean_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in clean]
    dirty_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in triggered]

    # make all the nans 0
    for mat in [*clean_adj_mats, *dirty_adj_mats]:
        mat[np.isnan(mat)] = 0

    print("starting to graph convert")
    clean_graphs = [ig.Graph.Weighted_Adjacency(x.tolist(), mode="undirected") for x in clean_adj_mats]
    dirty_graphs = [ig.Graph.Weighted_Adjacency(x.tolist(), mode="undirected") for x in dirty_adj_mats]
    print("finished graph convert")

    sample = len(clean_graphs)

    NUM_NEURONS = 1467

    c_f = []
    for x in tqdm(clean_graphs[:sample]):
        c_f.append(x.assortativity_degree(directed=False))

    d_f = []
    for x in tqdm(dirty_graphs[:sample]):
        d_f.append(x.assortativity_degree(directed=False))

    plt.hist(c_f, alpha=0.5, label='clean', color='green')
    plt.hist(d_f, alpha=0.5, label='dirty', color='red')
    plt.legend(loc='upper right')
    plt.show()

# graph_assortativity_degree()

def graph_density():
    print("WARNING WARNIGN this function doesn't use density but EDGE COUNT")
    clean_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in clean]
    dirty_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in triggered]

    # make all the nans 0
    for mat in [*clean_adj_mats, *dirty_adj_mats]:
        mat[np.isnan(mat)] = 0

    print("starting to graph convert")
    clean_graphs = [ig.Graph.Weighted_Adjacency(x.tolist(), mode="undirected") for x in clean_adj_mats]
    dirty_graphs = [ig.Graph.Weighted_Adjacency(x.tolist(), mode="undirected") for x in dirty_adj_mats]
    print("finished graph convert")

    sample = len(clean_graphs)

    c_f = []
    for x in tqdm(clean_graphs[:sample]):
        c_f.append(x.ecount())

    d_f = []
    for x in tqdm(dirty_graphs[:sample]):
        d_f.append(x.ecount()) # TODO this *should* be density, but smt is fricked here

    plt.hist(c_f, alpha=0.5, label='clean', color='green')
    plt.hist(d_f, alpha=0.5, label='dirty', color='red')
    plt.legend(loc='upper right')
    plt.show()

# graph_density()

def avg_clustering():

    SAMPLE = 20
    clean_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in clean]
    dirty_adj_mats = [np.array(x.fv["correlation_matrix"], dtype='float64') for x in triggered]

    # make all the nans 0
    for mat in [*clean_adj_mats, *dirty_adj_mats]:
        mat[np.isnan(mat)] = 0

    print("starting to graph convert")
    clean_graphs = [nx.from_numpy_matrix(x) for x in clean_adj_mats[:SAMPLE]]
    dirty_graphs = [nx.from_numpy_matrix(x) for x in dirty_adj_mats[:SAMPLE]]
    # dirty_graphs = [ig.Graph.Weighted_Adjacency(x.tolist(), mode="undirected") for x in dirty_adj_mats]
    print("finished graph convert")

    c_f = []
    for x in tqdm(clean_graphs):
        c_f.append(nx.average_clustering(x))

    d_f = []
    for x in tqdm(dirty_graphs):
        d_f.append(nx.average_clustering(x))

    plt.hist(c_f, alpha=0.5, label='clean', color='green')
    plt.hist(d_f, alpha=0.5, label='dirty', color='red')
    plt.legend(loc='upper right')
    plt.show()

avg_clustering()

def plot_persistence_diagram():

    def makeSparseDM(X, thresh):
        N = X.shape[0]
        D = pairwise_distances(X, metric='euclidean')
        [I, J] = np.meshgrid(np.arange(N), np.arange(N))
        I = I[D <= thresh]
        J = J[D <= thresh]
        V = D[D <= thresh]
        return sparse.coo_matrix((V, (I, J)), shape=(N, N)).tocsr()

    rips = ripser.Rips()

    # clean_PH_lists = [x.PH_list for x in clean]
    # dirty_PH_lists = [x.PH_list for x in triggered]

    # acc_clean = [ [], [] ]

    # # ph list is of the form: [H0, H1]
    # # take all the clean PH lists, and join them into a single list
    # for model in clean_PH_lists:
    #     # acc_clean[0] = np.array([*acc_clean[0], *model[0]])
    #     # acc_clean[1] = np.array([*acc_clean[1], *model[1]])
    #     print(model[0])

    # create a matrix D as the sum of all the correlation matrices for the clean models

    a = clean[0].fv["correlation_matrix"]
    a[np.isnan(a)] = 0

    b = clean[1].fv["correlation_matrix"]
    b[np.isnan(b)] = 0

    c = triggered[0].fv["correlation_matrix"]
    c[np.isnan(c)] = 0

    plt.matshow(a)
    plt.show()

    return
    D = triggered[0].fv["correlation_matrix"]
    for i in range(1, len(triggered)):
        D += triggered[i].fv["correlation_matrix"]
    D /= len(triggered)

    # replace all instances of Nan with 0
    D[np.isnan(D)] = 0
    D[np.isinf(D)] = 1

    D = makeSparseDM(D, 20)
    # plt.matshow(D)
    # plt.show()
    print(D)

    # lambdas=getGreedyPerm(D)
    # print("lambdas", lambdas)
    # D = getApproxSparseDM(lambdas, 2, D)
    # print(D)
    PH=rips.fit_transform(D, distance_matrix=True)
    rips.plot(PH)

    plt.show()



# plot_persistence_diagram()

def histogram_params():
    PARAM = 3
    for i in range(len(clean_fts[0])):

        a = sorted(clean_fts[:, i])[:-2]
        b = sorted(dirty_fts[:, i])[:-2]
        bins=np.histogram(np.hstack((a,b)), bins=40)[1] #get the bin edges


        plt.hist(a, bins = bins, alpha=0.5, label='clean', color='green')
# plt.hist(clean_fts[:, PARAM],  alpha=0.5, label='clean', color='green')
        plt.hist(b, bins = bins, alpha=0.5, label='dirty', color='red')
        plt.legend(loc='upper right')
        plt.show()


# histogram_params()



