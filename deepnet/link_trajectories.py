import numpy as np
import numpy.random as random
import scipy.optimize as opt
import TrkFile
import APT_interface as apt
import logging
import os
import scipy
import pickle

# for now I'm just using loadmat and savemat here
# when/if the format of trk files changes, then this will need to get fancier

from tqdm import tqdm
import torch
from torchvision import models
from torch import optim
import torch.nn.functional as F
import PoseTools
import movies
import tempfile
import copy
import multiprocessing as mp
from tqdm.contrib.concurrent import process_map
import hdf5storage

# for debugging
import matplotlib
from matplotlib import cm
import matplotlib.pyplot as plt
import cv2
import time
import scipy.spatial.distance as ssd
from scipy.cluster.hierarchy import linkage, dendrogram, fcluster


def angle_span(pcurr,pnext):
  z = pcurr-pnext
  y = np.linalg.norm(z,axis=1)
  an = np.arctan2(z[:,1],z[:,0])*180/np.pi
  an = an[y>3]

  sp = []
  incr = 10
  for c in np.arange(0,360,incr):
    an = np.mod(an+c,360)
    cur_sp = an.max()-an.min()
    sp.append(cur_sp)
  ang_span = min(sp) if len(sp)>1 else 0
  return ang_span

def match_frame(pcurr, pnext, idscurr, params, lastid=np.nan, maxcost=None,force_match=False,t=0):
  """
  match_frame(pcurr,pnext,idscurr,params,lastid=np.nan)
  Uses the Hungarian algorithm to match targets tracked in the current
  frame with targets detected in the next frame. The cost of
  assigning target i to detection j is the L1 error between the
  2*nlandmarks dimensional vectors normalized by the number of landmarks.
  The cost of a trajectory birth or death is params['maxcost']/2. Thus,
  it is preferable to kill one trajectory and create another if
  the matching error is > params['maxcost']
  Inputs:
  nlandmarks x d x ncurr positions of landmarks of nnext animals
  detected in the next frame
  idscurr: ncurr array, integer ids of the animals tracked in the
  current frame
  params: dictionary of parameters.
  lastid: (optional) scalar, last id used in tracking so far, if there are
  trajectory births, they will start with id lastid+1
  Outputs:
  idsnext: nnext array, integer ids assigned to animals in next frame
  Parameters:
  params['maxcost']: The cost of a trajectory birth or death is
  params['maxcost']/2. Thus, it is preferable to kill one trajectory
  and create another if the matching error is > params['maxcost'].
  params['verbose']: Whether to print out information
  """
  
  # pcurr: nlandmarks x d x ncurr
  # pnext: nlandmarks x d x nnext
  
  # check sizes
  nlandmarks = pcurr.shape[0]
  d = pcurr.shape[1]
  ncurr = pcurr.shape[2]
  nnext = pnext.shape[2]
  assert pnext.shape[0] == nlandmarks, \
    'N landmarks do not match, curr = %d, next = %d' % (nlandmarks, pnext.shape[0])
  assert pnext.shape[1] == d, \
    'Dimensions do not match, curr = %d, next = %d' % (d, pnext.shape[1])
  if maxcost is None:
    maxcost = params['maxcost']
  
  # construct the cost matrix
  # C[i,j] is the cost of matching curr[i] and next[j]
  C = np.zeros((ncurr+nnext, ncurr+nnext))
  C[:] = maxcost / 2.
  C[ncurr:, nnext:] = 0
  pcurr = np.reshape(pcurr, (d * nlandmarks, ncurr, 1))
  pnext = np.reshape(pnext, (d * nlandmarks, 1, nnext))
  C1 = np.sum(np.abs(pcurr-pnext), axis=0)/nlandmarks
  C[:ncurr, :nnext] = np.reshape(C1, (ncurr, nnext))

  strict_match_thres = params['strict_match_thres']

  if not force_match:
    # Don't do the ratio to second lowest match if force_match is on. This is used when estimating the maxcost parameter.

    # If a current detection has 2 matches then break the tracklet
    for x1 in range(ncurr):
      if np.all(np.isnan(C1[x1, :])): continue
      x1_curr = np.nanargmin(C1[x1, :])
      curc = C1.copy()
      c1 = curc[x1, x1_curr]
      curc[x1, x1_curr] = np.nan
      curc[np.isnan(curc)] = np.inf
      c2 = np.min(curc[x1, :])
      if (c2 / (c1 + 0.0001)) < strict_match_thres:
        C[x1, :nnext] = maxcost*2

    # If a next detection has 2 matches then break the tracklet
    for x1 in range(nnext):
      if np.all(np.isnan(C1[:,x1])): continue
      x1_curr = np.nanargmin(C1[:,x1])
      curc = C1.copy()
      c1 = curc[x1_curr,x1]
      curc[x1_curr,x1] = np.nan
      curc[np.isnan(curc)] = np.inf
      c2 = np.min(curc[:,x1])
      if (c2/(c1+0.0001)) < strict_match_thres:
        C[:ncurr,x1] = maxcost*2

    for x1 in range(ncurr):
      for x2 in range(nnext):
        has_match = np.any(C[x1,:nnext] < maxcost) or np.any(C[:ncurr,x2]<maxcost)
        if has_match: continue
        if maxcost<C[x1,x2]<maxcost*1.95:
          p1 = np.reshape(pcurr[:,x1,0],[nlandmarks,d])
          p2 = np.reshape(pnext[:,0,x2],[nlandmarks,d])
          a_span = angle_span(p1,p2)
          if a_span<=180:
            red_factor = max(a_span,90)/180
            C[x1,x2] = C[x1,x2]*red_factor


  # match
  idxcurr, idxnext = opt.linear_sum_assignment(C)

  costs = C[idxcurr, idxnext]
  cost = np.sum(costs)
  
  # idxnext < nnext, idxcurr < ncurr means we are assigning
  # an existing id
  idsnext = -np.ones(nnext, dtype=int)
  isassigned = np.logical_and(idxnext < nnext, idxcurr < ncurr)
  idsnext[idxnext[isassigned]] = idscurr[idxcurr[isassigned]]
  
  # idxnext < nnext, idxcurr >= ncurr means we are creating
  # a new trajectory
  if np.isnan(lastid):
    lastid = np.max(idscurr)
  idxbirth = idxnext[np.logical_and(idxnext < nnext, idxcurr >= ncurr)]
  for i in range(np.size(idxbirth)):
    lastid += 1
    idsnext[idxbirth[i]] = lastid
  
  if params['verbose'] > 1:
    isdeath = np.logical_and(idxnext >= nnext, idxcurr < ncurr)
    logging.info('N. ids assigned: %d, N. births: %d, N. deaths: %d' % (
      np.count_nonzero(isassigned), np.size(idxbirth), np.count_nonzero(isdeath)))
  
  return idsnext, lastid, cost, costs

def assign_ids(trk, params, T=np.inf):
  """
  assign_ids(trk,params)
  Assign identities to each detection in each frame so that one-to-one
  inter-frame match cost is minimized. Matching between frames t and t+1
  is done using match_frame.
  Input:
  trk: Trk object, where Trk.pTrk[:,:,:,t] are the
  detections for frame t. All coordinates will be nan if the number of
  detections in a given frame is less than maxnanimals.
  params: dictionary of parameters (see match_frame for details).
  Output: ids is a Tracklet representation of a maxnanimals x T matrix with
  integers 0, 1, ... indicating the identity of each detection in each frame.
  -1 is assigned to dummy detections.
  """
  
  # p is d x nlandmarks x maxnanimals x T
  # nan is used to indicate missing data
  T = int(np.minimum(T, trk.T))
  T1 = trk.T0+T-1
  pcurr = trk.getframe(trk.T0)
  idxcurr = trk.real_idx(pcurr)
  pcurr = pcurr[:, :, idxcurr]
  ids = TrkFile.Tracklet(defaultval=-1, size=(1, trk.ntargets, T))
  # allocate for speed!
  [sf, ef] = trk.get_startendframes()
  ids.allocate((1,), sf-trk.T0, np.minimum(T-1, ef-trk.T0))
  # ids = -np.ones((trk.T,trk.ntargets),dtype=int)
  idscurr = np.arange(np.count_nonzero(idxcurr), dtype=int)
  
  ids.settargetframe(idscurr, np.where(idxcurr.flatten())[0], 0)
  # ids[idxcurr,0] = idscurr
  if idscurr.size == 0:
    lastid = 0
  else:
    lastid = np.max(idscurr)
  costs = np.zeros(T-1)
  
  set_default_params(params)
  
  for t in tqdm(range(trk.T0, T1+1)):
    pnext = trk.getframe(t)
    idxnext = trk.real_idx(pnext)
    pnext = pnext[:, :, idxnext]
    idsnext, lastid, costs[t-1-trk.T0], _ = \
      match_frame(pcurr, pnext, idscurr, params, lastid,t=t)
    ids.settargetframe(idsnext, np.where(idxnext.flatten())[0], t-trk.T0)
    # ids[t,idxnext] = idsnext
    pcurr = pnext
    idscurr = idsnext
  return ids, costs

def dummy_ids(trk):
  T = int(trk.T)
  ids = TrkFile.Tracklet(defaultval=-1, size=(1, trk.ntargets, T))
  # allocate for speed!
  [sf, ef] = trk.get_startendframes()
  ids.allocate((1,), sf - trk.T0, np.minimum(T - 1, ef - trk.T0))
  for t in range(trk.ntargets):
    curid = np.ones(ef[t]-sf[t]+1)*t
    ids.settargetframe(curid, t, np.arange(sf[t]-trk.T0,ef[t]-trk.T0+1))
  return ids


def match_frame_id(pcurr, pnext, idcost, params, defaultval=np.nan):
  """
  match_frame_id(pcurr,pnext,idcost,params,maxcost=None)
  Uses the Hungarian algorithm to match targets tracked in the current
  frame with targets detected in the next frame. The cost of
  assigning target i to detection j is the L1 error between the
  2*nlandmarks dimensional vectors normalized by the number of landmarks.
  The cost of a trajectory birth or death is params['maxcost']/2. Thus,
  it is preferable to kill one trajectory and create another if
  the matching error is > params['maxcost']
  Inputs:
  d x nlandmarks x ncurr positions of landmarks of nnext animals
  detected in the next frame
  idscurr: ncurr array, integer ids of the animals tracked in the
  current frame
  params: dictionary of parameters.
  lastid: (optional) scalar, last id used in tracking so far, if there are
  trajectory births, they will start with id lastid+1
  Outputs:
  idsnext: nnext array, integer ids assigned to animals in next frame
  Parameters:
  params['maxcost']: The cost of a trajectory birth or death is
  params['maxcost']/2. Thus, it is preferable to kill one trajectory
  and create another if the matching error is > params['maxcost'].
  params['verbose']: Whether to print out information
  """
  
  # pcurr: d x nlandmarks x ntargets
  # pnext: d x nlandmarks x nnext
  # nlast: ntargets
  
  # check sizes
  nlandmarks = pcurr.shape[0]
  d = pcurr.shape[1]
  ntargets = pcurr.shape[-1]
  nnext = pnext.shape[-1]
  assert pnext.shape[0] == nlandmarks, \
    'N landmarks do not match, curr = %d, next = %d' % (nlandmarks, pnext.shape[0])
  assert pnext.shape[1] == d, \
    'Dimensions do not match, curr = %d, next = %d' % (d, pnext.shape[1])
  # which ids are assigned in the current frame
  idxcurr = TrkFile.real_idx(pcurr,defaultval).flatten()
  ncurr = np.count_nonzero(idxcurr)
  
  # construct the cost matrix
  # C[i,j] is the cost of matching curr[i] and next[j]
  C = np.zeros((ntargets+nnext, ntargets+nnext))
  # missing prediction
  C[:ntargets,nnext:] = params['cost_missing']
  # extra predictions
  C[ntargets:,:nnext] = params['cost_extra']
  pcurr = np.reshape(pcurr, (d * nlandmarks, ntargets, 1))
  pnext = np.reshape(pnext, (d * nlandmarks, 1, nnext))
  D = np.zeros((ntargets,nnext))
  D[idxcurr,:] = np.sum(np.abs(pcurr[:,idxcurr,:]-pnext), axis=0) / nlandmarks * params['weight_movement']
  Cmovement = C.copy()
  Cmovement[:ntargets, :nnext] = D
  C[:ntargets, :nnext] = D+idcost
  
  # match
  idxcurr, idxnext = opt.linear_sum_assignment(C)
  costs = C[idxcurr, idxnext]
  cost = np.sum(costs)
  
  # idxnext < nnext, idxcurr < ncurr means we are assigning
  # an existing id
  isassigned = np.logical_and(idxnext < nnext, idxcurr < ntargets)
  idsnext = -np.ones(nnext, dtype=int)
  idsnext[idxnext[isassigned]] = idxcurr[isassigned]

  ismissing = np.logical_and(idxnext >= nnext, idxcurr < ntargets)
  isextra = np.logical_and(idxnext < nnext, idxcurr >=ntargets)

  stats = {}
  stats['nmissing'] = np.count_nonzero(ismissing)
  stats['nextra'] = np.count_nonzero(isextra)
  stats['cost_movement'] = np.sum(Cmovement[idxcurr,idxnext])
  stats['cost_id'] = np.sum(idcost[idxcurr[isassigned],idxnext[isassigned]])
  stats['npred'] = nnext
  
  if params['verbose'] > 1:
    print('N. ids assigned: %d, N. extra detections: %d, N. missing detections: %d' % (
      np.count_nonzero(isassigned), stats['extra'], stats['nmissing']))
  
  return idsnext, cost, costs, stats

def assign_recognize_ids(trk, idcosts, params, T=np.inf):
  """
  assign_recognize_ids(trk,idcosts,params,T=inf)
  Assign identities to each detection in each frame so that both the one-to-one
  inter-frame match cost and the individual identity costs are minimized. Matching
  for frame t is done using match_frame_id.
  Input:
  trk: Trk object, where Trk.pTrk[:,:,:,t] are the
  detections for frame t. All coordinates will be nan if the number of
  detections in a given frame is less than maxnanimals.
  idcosts: list of length >= T, where idcosts[t] corresponds to frame t
  and idcosts[t][i,j] is the cost of assigning prediction i to target j.
  params: dictionary of parameters (see match_frame for details).
  T: scalar, number of frames to run assign for
  Output: ids is a Tracklet representation of a maxnanimals x T matrix with
  integers 0, 1, ... indicating the identity of each detection in each frame.
  -1 is assigned to dummy detections.
  """
  
  # p is d x nlandmarks x maxnanimals x T
  # nan is used to indicate missing data
  T = int(np.minimum(T, trk.T))
  T1 = trk.T0+T-1
  pcurr = trk.getframe(trk.T0)
  idxcurr = trk.real_idx(pcurr)
  pcurr = pcurr[:, :, idxcurr]
  ids = TrkFile.Tracklet(defaultval=-1, size=(1, T, trk.ntargets))
  # allocate for speed!
  [sf, ef] = trk.get_startendframes()
  ids.allocate((1,), sf, np.minimum(sf+T-1, ef))
    
  # idcosts is a len T list of ntargets x npreds[t] matrices
  ntargetsreal = idcosts[0].shape[0]
  costs = np.zeros(T)
  
  set_default_params(params)
  
  # save some statistics for debugging
  stats = {'nmissing': np.zeros(T, dtype=int), 'nextra': np.zeros(T, dtype=int), 'cost_movement': np.zeros(T),
           'cost_id': np.zeros(T), 'npred': np.zeros(T)}

  t = trk.T0
  pnext = trk.getframe(t)
  npts = pnext.shape[0]
  d = pnext.shape[1]
  npred = pnext.shape[3]
  pnext = pnext.reshape((npts,d,npred))

  # set ids in first frame based on idcosts only
  # idsnext[i] is which id prediction i was matched to
  idsnext,costs[0],_,statscurr = match_frame_id(np.zeros(pnext.shape),np.zeros(pnext.shape),idcosts[t-trk.T0],params,defaultval=trk.defaultval)
  ids.settargetframe(idsnext, np.where(idxcurr.flatten())[0], t)
  for key in statscurr.keys():
    stats[key][t-trk.T0] = statscurr[key]
    
  # initial nlast -- array storing number of frames since each id was last detected
  nlast = np.zeros(ntargetsreal,dtype=int)
  nlast[:] = params['maxframes_missed']
  pnext = pcurr
  
  for t in tqdm(range(trk.T0+1, T1+1)):
    
    # set pcurr based on pnext and idsnext from previous time point
    pcurr[:,:,idsnext[idsnext>=0]] = pnext[:,:,idsnext>=0]
    isdetected = np.isin(np.arange(ntargetsreal,dtype=int),idsnext)
    nlast += 1
    nlast[isdetected] = 0
    # only set pcurr to nan if it's been a very long time since we last detected this target
    pcurr[:,:,nlast>params['maxframes_missed']] = np.nan

    # read in the next frame positions
    pnext = trk.getframe(t)
    isnext = trk.real_idx(pnext)
    pnext = pnext[:, :, isnext]
    # main matching
    idsnext, costs[t-trk.T0], _, statscurr = \
      match_frame_id(pcurr, pnext, idcosts[t-trk.T0], params,defaultval=trk.defaultval)
    for key in statscurr.keys():
      stats[key][t-trk.T0] = statscurr[key]
    ids.settargetframe(idsnext, np.where(isnext.flatten())[0], t)

  if params['verbose'] > 0:
    print('Frames analyzed: %d, Extra detections: %d, Missed detections: %d'%(T,np.sum(stats['nextra']),np.sum(stats['nmissing'])))
    print('Frames with both extra and missed detections: %d'%(np.count_nonzero(np.logical_and(stats['nextra']>0,stats['nmissing']>0))))
    print('N. predictions: min: %d, mean: %f, max: %d'%(np.min(stats['npred']),np.mean(stats['npred']),np.max(stats['npred'])))
    prctiles_compute = [5.,10.,25.,50.,75.,90.,95.]
    cost_movement_prctiles = np.percentile(stats['cost_movement'],prctiles_compute)
    cost_id_prctiles = np.percentile(stats['cost_id'],prctiles_compute)
    print('Percentiles of movement, id cost:' )
    for i in range(len(prctiles_compute)):
      print('%dth percentile: %f, %f'%(prctiles_compute[i],cost_movement_prctiles[i],cost_id_prctiles[i]))

  return ids, costs, stats


def stitch(trk, ids, params):
  """
  stitch(trk,ids,params): Fill in short gaps (<= params['maxframes_missed']) to
  connect trajectory deaths and births.
  :param trk: Trk class object with detections
  :param ids: Tracklet class object indicating ids assigned to each detection, output of assign_ids
  :param params: parameters dict. Only relevant parameter is 'maxframes_missed'
  :return: ids: Updated identity assignment matrix after stitching
  :return: isdummy: Tracklet class object representing nids x T matrix indicating whether a frame is missed for a given id.
  """
  _, maxv = ids.get_min_max_val()
  nids = np.max(maxv)+1

  # get starts and ends for each id
  t0s = np.zeros(nids, dtype=int)
  t1s = np.zeros(nids, dtype=int)
  for id in range(nids):
    idx = ids.where(id)
    # idx = np.nonzero(id==ids)
    t0s[id] = np.min(idx[1])
    t1s[id] = np.max(idx[1])
  
  # isdummy = np.zeros((ids.ntargets,ids.T),dtype=bool)
  isdummy = TrkFile.Tracklet(defaultval=False, size=(1, nids, ids.T))
  isdummy.allocate((1,), t0s, t1s)
  
  allt1s = np.unique(t1s)
  assert allt1s[-1] == ids.T-1
  # skip deaths in last frame
  for i in range(len(allt1s)-1):
    t = allt1s[i]
    # all ids that end this frame
    ids_death = np.nonzero(t1s == t)[0]
    idscurr = ids.getframe(t)
    assert idscurr.shape[0]==1 and idscurr.shape[1]==1, 'Values returned by getframe have shape (1,1,ntgt)'
    if ids_death.size == 0:
      continue
    lastid = np.max(ids_death)
    pcurr = np.zeros((trk.nlandmarks, trk.d, ids_death.size))
    assert np.any(isdummy.gettargetframe(ids_death, t)) == False
    
    for j in range(ids_death.size):
      pcurr[:, :, j] = trk.gettargetframe(np.where(idscurr == ids_death[j])[2], t+trk.T0).reshape((trk.nlandmarks, trk.d))
      # pcurr[:,:,j] = p[:,:,ids[:,t]==ids_death[j],t].reshape((d,nlandmarks))
    for nframes_skip in range(2, params['maxframes_missed']+2):
      # all ids that start at frame t+nframes_skip
      ids_birth = np.nonzero(t0s == t+nframes_skip)[0]
      if ids_birth.size == 0:
        continue
      assert np.any(isdummy.gettargetframe(ids_birth, t+nframes_skip)) == False
      # assert np.any(isdummy[ids_birth,t+nframes_skip])==False
      pnext = np.zeros((trk.nlandmarks, trk.d, ids_birth.size))
      for j in range(ids_birth.size):
        pnext[:, :, j] = trk.gettargetframe(np.where(ids.getframe(t+nframes_skip) == ids_birth[j])[2],
                                            t+nframes_skip+trk.T0).reshape((trk.nlandmarks, trk.d))
        # pnext[:,:,j]=p[:,:,ids[:,t+nframes_skip]==ids_birth[j],t+nframes_skip].reshape((d,nlandmarks))
      # try to match
      maxcost = params['maxcost_missed'][np.minimum(params['maxcost_missed'].size-1, nframes_skip-2)]
      idsnext, _, _, _ = match_frame(pcurr, pnext, ids_death, params, lastid, maxcost=maxcost)
      # idsnext[j] is the id assigned to ids_birth[j]
      ismatch = idsnext <= lastid
      if not np.any(ismatch):
        continue
      for j in range(idsnext.size):
        id_death = idsnext[j]
        if id_death > lastid:
          continue
        id_birth = ids_birth[j]
        ids.replace(id_birth, id_death)
        # ids[ids==id_birth] = id_death
        idx = np.nonzero(ids_death == id_death)
        pcurr = np.delete(pcurr, idx[0], axis=2)
        ids_death = np.delete(ids_death, idx[0])
        t0s[id_birth] = -1
        t1s[id_death] = t1s[id_birth]
        t1s[id_birth] = -1
        isdummy.settargetframe(np.ones((1, nframes_skip-1), dtype=bool), id_death,
                               np.arange(t+1, t+nframes_skip, dtype=int))
        # isdummy[id_death,t+1:t+nframes_skip] = True
        if params['verbose'] > 0:
          logging.info('Stitching id %d frame %d to id %d frame %d' % (id_death, t, id_birth, t+nframes_skip))
      
      if ids_death.size == 0:
        break
  
  return ids, isdummy


def delete_short(ids, isdummy, params):
  """
  delete_short(ids,params):
  Delete trajectories that are at most params['maxframes_delete'] frames long.
  :param ids: maxnanimals x T matrix indicating ids assigned to each detection, output of assign_ids, stitch
  :param isdummy: nids x T matrix indicating whether a frame is missed for a given id.
  :param params: parameters dict. Only relevant parameter is 'maxnframes_delete'
  :return: ids: Updated identity assignment matrix after deleting
  """
  
  _, maxv = ids.get_min_max_val()
  nids = np.max(maxv)+1
  # nids=np.max(ids)+1
  
  # get starts and ends for each id
  t0s = -np.ones(nids, dtype=int)
  t1s = -np.ones(nids, dtype=int)
  nframes = np.zeros(nids, dtype=int)
  for id in range(nids):
    idx = ids.where(id)
    if not np.any(idx[1]):
      continue
    t0s[id] = np.min(idx[1])
    t1s[id] = np.max(idx[1])
    isdummycurr = isdummy.gettargetframe(id, np.arange(t0s[id], t1s[id]+1, dtype=int))
    nframes[id] = np.count_nonzero(isdummycurr == False)
  ids_short = np.nonzero(np.logical_and(nframes <= params['maxframes_delete'], t0s >= 0))[0]
  for id in ids_short:
    ids.replace(id, -1)
  # ids[np.isin(ids,ids_short)] = -1
  if params['verbose'] > 0:
    logging.info('Deleting %d short trajectories' % ids_short.size)
  return ids, ids_short


def delete_lowconf(trk, ids, params):
  """
  delete_lowconf(ids,params):
  Delete trajectories that have mean confidence lower than params['minconf_delete'] frames long.
  :param ids: maxnanimals x T matrix indicating ids assigned to each detection, output of assign_ids, stitch
  :param isdummy: nids x T matrix indicating whether a frame is missed for a given id.
  :param params: parameters dict. Only relevant parameter is 'maxnframes_delete'
  :return: ids: Updated identity assignment matrix after deleting
  """

  _, maxv = ids.get_min_max_val()
  nids = np.max(maxv) + 1
  tot_conf = np.zeros(nids)
  tot_count = np.zeros(nids)
  sf,ef = trk.get_startendframes()

  for tid in range(trk.ntargets):
    _,edict = trk.gettarget(tid,True)
    cur_ids = ids.gettarget(tid)
    assert cur_ids.shape[0]==1 and cur_ids.shape[2] == 1, 'Ids returned should have shape (1,nframes,1)'
    cur_ids = cur_ids[0,:,0][sf[tid]:(ef[tid]+1)]
    cur_conf = edict['pTrkConf'].mean(axis=0)
    cur_conf = cur_conf[(sf[tid]-trk.T0):(ef[tid]+1-trk.T0)]
    for j in range(nids):
      tot_conf[j] += np.nansum(cur_conf[cur_ids==j])
      tot_count[j] += np.nansum(cur_conf[cur_ids==j]>0)
  mean_conf = tot_conf/(tot_count+0.00001)
  ids_lowconf = np.nonzero(mean_conf<params['minconf_delete'])[0]
  for id in ids_lowconf:
    ids.replace(id, -1)
  if params['verbose'] > 0:
    logging.info('Deleting %d trajectories with low confidence' % ids_lowconf.size)
  return ids, ids_lowconf


def merge(trk,ids):
  p_ndx = min(ids)
  trk.pTrk[:, :, :, p_ndx] = np.nanmean(trk.pTrk[...,ids],-1)
  to_remove = ([i for i in ids if i!=p_ndx])

  trk.pTrk = np.delete(trk.pTrk,to_remove,-1)
  for k in trk.trkFields:
    if trk.__dict__[k] is not None:
      trk.__dict__[k] = np.delete(trk.__dict__[k],to_remove,-1)

  trk.ntargets = trk.ntargets-len(to_remove)


def merge_close(trk, params):
  """
  merge_close(trk,params):
  Delete trajectories that have are on average closer than params['maxcost'].
  :param params: parameters dict. Only relevant parameter is 'maxcost'
  """

  rm_count = 0
  orig_count = trk.ntargets
  while True:
    dist_trk = np.nanmean(np.abs(trk.pTrk[...,None,:]-trk.pTrk[...,None]).sum(1).mean(0),axis=0)
    dist_trk[np.diag_indices(dist_trk.shape[0])] = np.inf
    id1,id2 = np.unravel_index(np.nanargmin(dist_trk), dist_trk.shape)
    if dist_trk[id1,id2]>params['maxcost']:
      break
    merge(trk,[id1,id2])
    rm_count +=1

  logging.info(f'Removing {rm_count} out of {orig_count} trajectories by merging them into other trajectories that are close')


def estimate_maxcost(trks, params, params_in=None, nsample=1000, nframes_skip=1):
  if type(trks) not in [list,tuple]:
    trks = [trks]
  if params_in is not None:
    params.update(params_in)

  allcosts = []
  for trk in trks:
    allcosts.append(estimate_maxcost_ind(trk, params, nsample=nsample, nframes_skip=nframes_skip))
  allcosts = np.concatenate(allcosts,axis=0)

  mult = params['maxcost_mult']
  heuristic = params['maxcost_heuristic']
  prctile = params['maxcost_prctile']
  secondorder_thresh = params['maxcost_secondorder_thresh']

  if heuristic =='prctile':
    mult = 1.2
  #   mult = 100. / prctile
  # else:
  #   mult = 1.2

  if allcosts.size==0:
    maxcost = 10
  elif heuristic == 'prctile':
    maxcost = mult * np.percentile(allcosts, prctile)
  elif heuristic == 'secondorder':

    # use sharp increase in 2nd order differences.
    isz = 4.
    xx = np.arange(50, 100, 1 / isz)
    qq = np.percentile(allcosts, xx)

    ix, knee_val = PoseTools.find_knee(xx,qq)
    # The knee isn't too far from the diagonal, then don't use knee
    if knee_val<0.2:
      ix = 198

    # dd1 = qq[1:] - qq[:-1]
    # dd2 = dd1[1:] - dd1[:-1]
    # all_ix = np.where(dd2 > secondorder_thresh)[0]
    # # threshold is where the second order increases by 4, so sort of the coefficient for the quadratic term.
    # if len(all_ix) < 1:
    #   ix = 198  # choose 98 % as backup
    # else:
    #   ix = all_ix[0]
    # ix = np.clip(ix, 5, 198) + 1

    maxcost = mult * qq[ix]

    logging.info('nframes_skip = %d, choosing %f percentile of link costs with a value of %f to decide the maxcost' % (
    nframes_skip, ix / isz + 50, maxcost))

  return maxcost


def estimate_maxcost_ind(trk, params, nsample=1000, nframes_skip=1):
  """
  maxcost = estimate_maxcost(trk,nsample=1000,prctile=95.,mult=None,nframes_skip=1,heuristic='secondorder')
  Estimate the threshold for the maximum cost for matching identities. This is done
  by running match_frame on some sample frames, looking at the assignment costs
  assuming all assignments are allowed, and then taking a statistic of all those
  assignment costs.
  The heuristic used is maxcost = 2.* mult .* percentile(allcosts,prctile)
  where prctile and mult are parameters
  :param trk: Trk object
  :param nsample: Number of frames to sample, default = 1000
  :param prctile: Percentile used when computing threshold, default = 95.
  :param mult: Multiplier used when computing threshold , default = 100./prctile
  :param nframes_skip: Number of frames to skip, default = 1
  :param heuristic: How to convert statistics of costs to a threshold.
  Options: 'secondorder' (Mayank's heuristic), 'prctile' (Kristin's heuristic).
  Default: 'secondorder'.
  Returns threshold on cost.
  """

  nsample = np.minimum(trk.T, nsample)
  tsample = np.round(np.linspace(trk.T0, trk.T1-nframes_skip-1, nsample)).astype(int)
  minv, maxv = trk.get_min_max_val()
  minv = np.min(minv, axis=0)
  maxv = np.max(maxv, axis=0)
  if np.all(maxv==None) or np.all(minv==None):
    return np.zeros(0)
  bignumber = np.sum(maxv-minv) * 2.1
  # bignumber = np.sum(np.nanmax(p,axis=(1,2,3))-np.nanmin(p,axis=(1,2,3)))*2.1
  allcosts = np.zeros((trk.ntargets, nsample))
  allcosts[:] = np.nan

  for i in range(nsample):
    t = tsample[i]
    pcurr = trk.getframe(t)
    pnext = trk.getframe(t+nframes_skip)
    pcurr = pcurr[:, :, trk.real_idx(pcurr)]
    pnext = pnext[:, :, trk.real_idx(pnext)]
    if (pcurr.size<1) or (pnext.size<1): continue
    ntargets_curr = pcurr.shape[2]
    ntargets_next = pnext.shape[2]
    idscurr = np.arange(ntargets_curr)
    idsnext, _, _, costscurr = match_frame(pcurr, pnext, idscurr, params,force_match=True, maxcost=bignumber)
    ismatch = np.isin(idscurr, idsnext)
    assert np.count_nonzero(ismatch) == np.minimum(ntargets_curr, ntargets_next)
    costscurr = costscurr[:ntargets_curr]
    allcosts[:np.count_nonzero(ismatch), i] = costscurr[ismatch]
  
  isdata = np.isnan(allcosts) == False

  return allcosts[isdata]

  # debug code -- what are the differences between having no threshold on cost and having the chosen threshold
  # params['maxcost'] = maxcost
  #
  # for i in range(nsample):
  #     t=tsample[i]
  #     pcurr=p[:,:,:,t]
  #     pnext=p[:,:,:,t+1]
  #     pcurr=pcurr[:,:,real_idx(pcurr)]
  #     pnext=pnext[:,:,real_idx(pnext)]
  #     ntargets_curr=pcurr.shape[2]
  #     ntargets_next=pnext.shape[2]
  #     idscurr=np.arange(ntargets_curr)
  #     idsnext,_,_,costscurr=match_frame(pcurr,pnext,idscurr,params)
  #     ismatch=np.isin(idscurr,idsnext)
  #     nmiss = np.minimum(ntargets_curr,ntargets_next) - np.count_nonzero(ismatch)
  #     if nmiss > 0:
  #         sortedcosts = -np.sort(-allcosts[:,i])
  #         logging.info('i = %d, t = %d, nmiss = %d, ncurr = %d, nnext = %d, costs removed: %s'%(i,t,nmiss,ntargets_curr,ntargets_next,str(sortedcosts[:nmiss])))


def estimate_maxcost_missed(trk, params, nsample=1000):
  """
  maxcost_missed = estimate_maxcost_missed(trk,maxframes_missednsample=1000,prctile=95.,mult=None, heuristic='secondorder')
  Estimate the threshold for the maximum cost for matching identities across > 1 frame.
  This is done by running match_frame on some sample frames, looking at the assignment costs assuming all assignments
  are allowed, and then taking a statistic of all those assignment costs.
  The heuristic used is maxcost = 2.* mult .* percentile(allcosts,prctile)
  where prctile and mult are parameters.
  :param trk: Trk object
  :param maxframes_missed: How many frames can be skipped
  :param nsample: Number of frames to sample
  :param prctile: Percentile used when computing threshold
  :param mult: Multiplier used when computing threshold
  :param heuristic: How to convert statistics of costs to a threshold.
  Options: 'secondorder' (Mayank's heuristic), 'prctile' (Kristin's heuristic).
  Default: 'secondorder'.
  Returns np.ndarray containing threshold on cost for each number of frames missed.
  """

  maxframes_missed = params['maxcost_framesfit']
  maxcost_missed = np.zeros(maxframes_missed)
  for nframes_skip in range(2, maxframes_missed+2):
    maxcost_missed[nframes_skip-2] = estimate_maxcost(trk, params,  nframes_skip=nframes_skip, nsample=nsample)
  return maxcost_missed


def set_default_params(params):
  if 'verbose' not in params:
    params['verbose'] = 1
  if 'weight_movement' not in params:
    params['weight_movement'] = 1.
  if 'maxframes_missed' not in params:
    params['maxframes_missed'] = np.inf


def get_default_params(conf):
  # Update some of the parameters based on conf
  params = {}
  params['verbose'] = 1
  params['maxframes_missed'] = conf.link_maxframes_missed
  params['maxframes_delete'] = conf.link_maxframes_delete
  params['maxcost_prctile'] = conf.link_maxcost_prctile
  params['maxframes_sel'] = conf.link_id_min_tracklet_len
  params['maxcost_mult'] = conf.link_maxcost_mult
  params['maxcost_framesfit'] = conf.link_maxcost_framesfit
  params['maxcost_heuristic'] = conf.link_maxcost_heuristic
  params['maxcost_secondorder_thresh'] = conf.link_maxcost_secondorder_thresh
  params['minconf_delete'] = 0.5
  params['strict_match_thres'] = conf.link_strict_match_thres
  return params


def test_assign_ids():
  """
  test_assign_ids():
  constructs some synthetic data and makes sure assign_ids works
  """
  
  # random.seed(2)
  d = 2
  nlandmarks = 17
  n0 = 6
  minn = 3
  pbirth = .5
  pdeath = .5
  T = 20
  maxnbirthdeath = 2
  
  params = {}
  params['maxcost'] = .1
  # params['verbose'] = 1
  
  # create some data
  p = np.zeros((nlandmarks, d, T, n0))
  p[:] = np.nan
  ids = -np.ones((T, n0))
  
  pcurr = random.rand(nlandmarks, d, n0)
  p[:, :, 0, :] = pcurr
  idscurr = np.arange(n0)
  ids[0, :] = idscurr
  lastid = np.max(idscurr)
  
  for t in range(1, T):
    
    idxcurr = TrkFile.real_idx(pcurr,np.nan)
    ncurr = np.count_nonzero(idxcurr)
    pnext = pcurr[:, :, idxcurr]
    idsnext = idscurr
    for i in range(maxnbirthdeath):
      if ncurr > minn and random.rand(1) <= pdeath:
        pnext = pnext[:, :, :-1]
        idsnext = idsnext[:-1]
        logging.info('%d: death' % t)
    for i in range(maxnbirthdeath):
      if random.rand(1) <= pbirth:
        lastid += 1
        pnext = np.concatenate((pnext, random.rand(nlandmarks, d, 1)), axis=2)
        idsnext = np.append(idsnext, lastid)
        logging.info('%d: birth' % t)
    nnext = pnext.shape[2]
    if nnext > p.shape[3]:
      pad = np.zeros((nlandmarks, d, T, nnext-p.shape[3]))
      pad[:] = np.nan
      p = np.concatenate((p, pad), axis=3)
      ids = np.concatenate((ids, -np.ones((T, nnext-ids.shape[1]))), axis=1)
    perm = random.permutation(nnext)
    pnext = pnext[:, :, perm]
    idsnext = idsnext[perm]
    p[:, :, t, :nnext] = pnext
    ids[t, :nnext] = idsnext
    
    pcurr = pnext
    idscurr = idsnext
  
  logging.info('ids = ')
  logging.info(str(ids))
  ids1, costs = assign_ids(TrkFile.Trk(p=p), params)
  
  logging.info('assigned ids = ')
  logging.info(str(ids1))
  logging.info('costs = ')
  logging.info(str(costs))
  
  issameid = np.zeros((ids.shape[0]-1, ids.shape[1]**2))
  for t in range(ids.shape[0]-1):
    issameid[t, :] = (ids[t, :].reshape((ids.shape[1], 1)) == ids[t+1, :].reshape((1, ids.shape[1]))).flatten()
  
  ids1d = ids1.getdense()
  ids1d = ids1d.reshape((ids1d.shape[1:]))
  issameid1 = np.zeros((ids1d.shape[0]-1, ids1d.shape[1]**2))
  for t in range(ids1d.shape[0]-1):
    issameid1[t, :] = (ids1d[t, :].reshape((ids1d.shape[1], 1)) == ids1d[t+1, :].reshape((1, ids1d.shape[1]))).flatten()
  
  assert np.all(issameid1 == issameid)


def test_match_frame():
  """
  test_match_frame():
  constructs some synthetic data and makes sure match_frame works
  """
  
  d = 2
  nlandmarks = 17
  ncurr = 6
  nnext = ncurr+1
  
  pcurr = random.rand(d, nlandmarks, ncurr)
  pnext = np.zeros((d, nlandmarks, nnext))
  if nnext < ncurr:
    pnext = pcurr[:, :, :nnext]
  else:
    pnext[:, :, :ncurr] = pcurr
    pnext[:, :, ncurr:] = random.rand(d, nlandmarks, nnext-ncurr)
  
  idscurr = np.arange(0, ncurr)
  lastid = np.max(idscurr)
  
  perm = random.permutation(nnext)
  pnext = pnext[:, :, perm]
  
  params = {}
  params['maxcost'] = .8
  params['verbose'] = 1
  
  idsnext, lastid, cost, _ = match_frame(pcurr, pnext, idscurr, params, lastid)
  logging.info('permutation = '+str(perm))
  logging.info('idsnext = '+str(idsnext))
  logging.info('cost = %f' % cost)


def mixed_colormap(n, cmfun=cm.jet):
  idx0 = np.linspace(0., 1., n)
  cm0 = cmfun(idx0)
  
  d = np.abs(idx0.reshape((1, n))-idx0.reshape((n, 1)))
  idx = np.zeros(n, dtype=int)
  mind = d[0, :]
  mind[0] = -np.inf
  for i in range(1, n):
    j = np.argmax(mind)
    idx[i] = j
    mind = np.minimum(mind, d[j, :])
    mind[j] = -np.inf
  cm1 = cm0[idx, :]
  return cm1

def nonmax_supp(trk, params):
  for t in range(trk.T0,trk.T1+1):
    pcurr = trk.getframe(t)
    curd = np.abs(pcurr[...,0,:,None]-pcurr[...,0,None,:]).sum(1).mean(0)
    curd[np.diag_indices(curd.shape[0])] = np.inf
    if np.all(np.isnan(curd)|np.isinf(curd)): continue
    id1,id2 = np.where(curd<params['nms_max'])
    groups = []
    for ndx in range(len(id1)):
      done = False
      for g in groups:
        if g.count(id1[ndx])>0:
          done = True
          if g.count(id2[ndx])==0:
            g.append(id2[ndx])
        if g.count(id2[ndx])>0:
          done = True
          if g.count(id1[ndx])==0:
            g.append(id1[ndx])
      if not done:
        groups.append([id1[ndx],id2[ndx]])

    for g in groups:
      p_ndx = g[0]
      to_remove = g[1:]
      pcurr[...,0,p_ndx] = np.mean(trk.pTrk[:,:,t,g],axis=2)
      pcurr[...,0,to_remove] = np.nan
      trk.setframe(pcurr,t)


def link_pure(trk, conf, do_delete_short=False):
  """
  Does pure linking. Pure is meant to suggest that there is very little chance of two different animals to be part of the same tracklet. The linking criterion barrier is such that we link predictions only if there is a significant margin that the predictions belong to the same animal -- prediction p1 is joined to p2 only if dist(p1,p2) < 2*min(dist(p1,p_others)).

  :param trk:
  :type trk:
  :param conf:
  :type conf:
  :param do_delete_short:
  :type do_delete_short:
  :return:
  :rtype:
  """

  params = get_default_params(conf)

  if 'maxcost' not in params:
    params['maxcost'] = estimate_maxcost(trk, params)
  logging.info('maxcost set to %f' % params['maxcost'])

  if 'maxcost_missed' not in params:
    params['maxcost_missed'] = estimate_maxcost_missed(trk, params)
    logging.info('maxcost_missed set to ' + str(params['maxcost_missed']))

  params['maxframes_delete'] = conf.link_id_min_tracklet_len

  T = np.minimum(np.inf, trk.T)
  nframes_test = np.inf
  nframes_test = int(np.minimum(T, nframes_test))

  trk.convert2sparse()

  # Do the linking
  ids, costs = assign_ids(trk, params, T=nframes_test)

  _, maxv = ids.get_min_max_val()
  nids = np.max(maxv) + 1
  # nids = np.max(ids)+1

  # get starts and ends for each id
  t0s = np.zeros(nids, dtype=int)
  t1s = np.zeros(nids, dtype=int)
  for id in range(nids):
    idx = ids.where(id)
    if idx[0].size==0: continue
    # idx = np.nonzero(id==ids)
    t0s[id] = np.min(idx[1])
    t1s[id] = np.max(idx[1])

  # isdummy = np.zeros((ids.ntargets,ids.T),dtype=bool)
  isdummy = TrkFile.Tracklet(defaultval=False, size=(1, nids, ids.T))
  isdummy.allocate((1,), t0s, t1s)

  if do_delete_short:
    ids, ids_short = delete_short(ids, isdummy, params)
  #  if locs_conf is not None:
  #    ids,ids_lowconf = delete_lowconf(trk,ids,params)

  _, ids = ids.unique()
  trk.apply_ids(ids)
  return trk

  return l_trk

def link_trklets(trk_files, conf, movs, out_files):
  """
  Links pure tracklets using id liking or motion based on conf.link_id
  :param trk_files: trk files with pure linked trajectories
  :type trk_files: list of str
  :param conf:
  :type conf: poseConfig.config
  :param movs: movie files corresponding to the trk files
  :type movs: list of str
  :param out_files: Output files. The linked trajectories are not saved. The file names are used to save intermediate files for id tracking (wts, images etc).
  :type out_files: list of str
  :return: linked trk files
  :rtype: list
  """
  in_trks = [TrkFile.Trk(tt) for tt in trk_files]

  if conf.link_id:
    conf1 = copy.deepcopy(conf)
    ww = conf1.multi_animal_crop_sz
    conf1.imsz = [ww,ww]

    if len(conf1.ht_pts)>0 and conf1.ht_pts[0]>=0:
      conf1.use_ht_trx = True
      conf1.use_bbox_trx = False
      conf1.trx_align_theta = True
    else:
      logging.warning('Head-Tail points are not defined. Assigning identity without aligning the animals!!')
      conf1.use_bbox_trx = True
      conf1.use_ht_trx = False
      conf1.trx_align_theta = False
    return link_id(in_trks, trk_files, movs, conf1, out_files)

  else:
    params = get_default_params(conf)

    if 'maxcost' not in params:
      params['maxcost'] = estimate_maxcost(in_trks, params)
    logging.info('maxcost set to %f' % params['maxcost'])

    if 'maxcost_missed' not in params:
      params['maxcost_missed'] = estimate_maxcost_missed(in_trks, params)
      logging.info('maxcost_missed set to ' + str(params['maxcost_missed']))

    params['maxframes_delete'] = conf.link_id_min_tracklet_len

    # if 'nms_max' not in params:
    # params['nms_max'] = estimate_maxcost(trk, prctile=params['nms_prctile'], mult=1, heuristic='prctile')

    #  nonmax_supp(trk, params)
    out_trks = [link(trk,params) for trk in in_trks]
    return out_trks


def link(trk,params,do_merge_close=False,do_stitch=True,do_delete_short=False):
  '''

  :param trk: trk object
  :type trk: TrkFile.Trk
  :param params: linking parameters
  :type params:
  :param do_merge_close: whether to merge trajectories that are close or not
  :type do_merge_close: bool
  :param do_stitch: whether to do stitching across frames
  :type do_stitch: bool
  :param do_delete_short: to delete short trajectories
  :type do_delete_short: bool
  :return: linked trajectory
  :rtype: TrkFile.Trk
  '''

  if trk.ntargets ==0:
    return trk

  ids = dummy_ids(trk)

  if do_stitch:
    ids, isdummy = stitch(trk, ids, params)
  else:
    _, maxv = ids.get_min_max_val()
    nids = np.max(maxv) + 1
    # nids = np.max(ids)+1

    # get starts and ends for each id
    t0s = np.zeros(nids, dtype=int)
    t1s = np.zeros(nids, dtype=int)
    for id in range(nids):
      idx = ids.where(id)
      # idx = np.nonzero(id==ids)
      t0s[id] = np.min(idx[1])
      t1s[id] = np.max(idx[1])

    # isdummy = np.zeros((ids.ntargets,ids.T),dtype=bool)
    isdummy = TrkFile.Tracklet(defaultval=False, size=(1, nids, ids.T))
    isdummy.allocate((1,), t0s, t1s)

  if do_delete_short:
    ids, ids_short = delete_short(ids, isdummy, params)
#  if locs_conf is not None:
#    ids,ids_lowconf = delete_lowconf(trk,ids,params)
  _, ids = ids.unique()
  trk.apply_ids(ids)
  if do_merge_close:
    merge_close(trk,params)
  return trk


def link_id(trks, trk_files, mov_files, conf, out_files, id_wts=None):
  '''
  Link traj. based on identity
  :param trks:
  :type trks:
  :param trk_files:
  :type trk_files:
  :param mov_files:
  :type mov_files:
  :param conf:
  :type conf:
  :param out_files:
  :type out_files:
  :return:
  :rtype:
  '''


  all_trx = []

  for trk_file, mov_file in zip(trk_files,mov_files):
    # Read the trk files as trx. The trx are required to generate animal examles.
    cap = movies.Movie(mov_file)
    trx_dict = apt.get_trx_info(trk_file, conf, cap.get_n_frames(),use_ht_pts=True)
    trx = trx_dict['trx']
    all_trx.append(trx)
    cap.close()

  if id_wts is not None and os.path.exists(id_wts):
    id_classifier = load_id_wts(id_wts)
  else:
  # generate the training images
    train_data = get_id_train_images(trks, all_trx, mov_files, conf)
    wt_out_file = out_files[0].replace('.trk','_idwts.p')
    # train the identity model
    id_classifier, loss_history = train_id_classifier(train_data,conf, trks, save_file=wt_out_file,bsz=conf.link_id_batch_size)

  # link using id model
  def_params = get_default_params(conf)
  trk_out = link_trklet_id(trks,id_classifier,mov_files,conf, all_trx,min_len_select=def_params['maxframes_sel'],keep_all_preds=conf.link_id_keep_all_preds)
  return trk_out


def get_id_train_images(linked_trks, all_trx, mov_files, conf):
  '''
  Generate id training images.
  :param linked_trks:
  :type linked_trks:
  :param all_trx:
  :type all_trx:
  :param mov_files:
  :type mov_files:
  :param conf:
  :type conf:
  :return:
  :rtype:
  '''
  all_data = []
  for trk, trx, mov_file in zip(linked_trks,all_trx,mov_files):
    ss, ee = trk.get_startendframes()

    # ignore small tracklets
    min_trx_len = conf.link_id_min_train_track_len

    # incase all traj are small
    if np.count_nonzero((ee-ss+1)>min_trx_len)<conf.max_n_animals:
      min_trx_len = min(1,np.percentile((ee-ss+1),20)-1)

    sel_trk = np.where((ee - ss+1) > min_trx_len)[0]
    sel_trk_info = list(zip(sel_trk, ss[sel_trk], ee[sel_trk]))

    data = read_ims_par(trx, sel_trk_info, mov_file, conf)
    # data = read_data_files(data_files)
    all_data.append(data)
  return all_data

def get_overlap(ss_t,ee_t,ss,ee, curidx):
  # For overlap either the start of the trajectory should lie within the range or the end
  # Since trk ends go to last frame + 1, less and greater comparisons have to be done carefully
  starts = np.maximum(ss_t,ss)
  ends = np.minimum(ee_t+1,ee+1)
  overlap_amt = np.array([len(range(st,en))/(ee-ss+1) for st,en in zip(starts,ends)])
  overlap_tgts = np.where(overlap_amt>0)[0]
  overlap_tgts = np.array(list(set(overlap_tgts) - set([curidx])))

  if overlap_tgts.size == 0:
    overlap_amt = np.array([])
  else:
    overlap_amt = overlap_amt[overlap_tgts]

  # overlaps = ((ss_t >= ss) & (ss_t <  ee)) | \
  #            ((ee_t >  ss) & (ee_t <= ee)) | \
  #            ((ss >= ss_t) & (ss <  ee_t)) | \
  #            ((ee >  ss_t) & (ee <= ee_t))
  # overlap_tgts = np.where(overlaps)[0]
  # overlap_tgts = np.array(list(set(overlap_tgts) - set([curidx])))
  return overlap_tgts, overlap_amt


class id_dset(torch.utils.data.IterableDataset):
  """
  Data generator that generates difficult training examples using mining
  """

  def __init__(self, all_data, mining_dists, trk_data, confd, rescale, valid, distort=True, debug=False):

      self.all_data = [all_data, mining_dists, trk_data, confd, rescale, valid, distort]
      self.debug = debug

  def __iter__(self):
    [all_data, mining_dists, trk_data, confd, rescale, valid, distort] = self.all_data

    while True:
      curims = []
      sel_ndx = np.random.randint(len(all_data))
      data = all_data[sel_ndx]
      dists, overlap_dist_mean, self_dist_mean = mining_dists[sel_ndx]
      ss_t, ee_t, _ = trk_data[sel_ndx]
      n_tr = len(data)

      info = []
      while len(curims) < 1:

        # Select principal tracklet with equal prob. based on 1) overall how far the samples from same tracklet are. 2) overall how close the images are to overlapping tracklets.

        # self_dist_mean is small for a tracklet if its images are close to each other (not that useful for training), and large if images are far from each other (useful for training)
        # overlap_dist_mean is large for a tracklet if its images are far from other tracklets (not that useful for training), and small if images are close to iamges of ovrlapping tracklets (useful for training)

        if np.random.rand() < 0.5:
          self_dist1 = self_dist_mean+0.2
          sample_wt = self_dist1 / self_dist1.sum()
        else:
          sample_wt = 2.2 - np.clip(overlap_dist_mean, 0, 2)
          sample_wt = sample_wt / sample_wt.sum()

        curidx = np.random.choice(n_tr, p=sample_wt)
        cur_dat = data[curidx]

        if not valid:
          # Dummy data. To be used during early part of the training
          overlap_tgts, overlap_amt = get_overlap(ss_t,ee_t,ss_t[curidx],ee_t[curidx],curidx)
          t_dist_all = np.ones([len(overlap_tgts),cur_dat[0].shape[0], cur_dat[0].shape[0]])
          t_dist_self = np.ones([cur_dat[0].shape[0], cur_dat[0].shape[0]])
        else:
          overlap_tgts, overlap_amt = dists[curidx][4:6]
          t_dist_self = dists[curidx][0]
          t_dist_all = dists[curidx][1]

        # no overlapping tracklet. can't be used for training
        if overlap_tgts.size < 1: continue

        # Choose the first image such that image that is away from others has higher prob. 0.2 is added so that we don't end up concentrating on a small set in pathological cases
        wt_self = (t_dist_self + 0.2).sum(axis=1)
        wt_self = wt_self / wt_self.sum()
        idx_self1 = np.random.choice(len(cur_dat[0]), p=wt_self)
        im1 = cur_dat[0][idx_self1]

        # Choose the second image such that image that is away from the first one has higher prob.
        wt_self2 = t_dist_self[idx_self1] + 0.2
        wt_self2 = wt_self2 / wt_self2.sum()
        idx_self2 = np.random.choice(len(cur_dat[0]), p=wt_self2)
        im2 = cur_dat[0][idx_self2]

        # Select the 3 image from overlaping tracklet such that images that are close to both 1 and 2 have higher prob.
        t_dist_overlap_idx = (t_dist_all[:,idx_self1] + t_dist_all[:,idx_self2]) / 2
        overlap_wts = 2.2 - np.clip(t_dist_overlap_idx, 0, 2)
        overlap_wts = overlap_wts*overlap_amt[:,None]
        o_sh = overlap_wts.shape
        overlap_wts = overlap_wts.flatten()
        overlap_sel = np.random.choice(len(overlap_wts), p=overlap_wts / overlap_wts.sum())
        overlap_tgt_ndx, overlap_im_idx = np.unravel_index(overlap_sel, o_sh)
        overlap_tgt = overlap_tgts[overlap_tgt_ndx]

        overlap_im = data[overlap_tgt][0][overlap_im_idx]

        # Do an overlap check
        check = np.zeros(cur_dat[3]-cur_dat[2]+1)
        odata = data[overlap_tgt]
        over_sf = np.maximum(0,odata[2]-cur_dat[2])
        over_ef = np.minimum(cur_dat[3]-cur_dat[2]+1, odata[3]-cur_dat[2]+1)
        check[over_sf:over_ef] = 1

        if check.sum()<1:
          logging.info(f'mov:{sel_ndx}, tr1:{cur_dat[1]}:{cur_dat[2]}-{cur_dat[3]} im1:{cur_dat[4][idx_self1][0]} im2:{cur_dat[4][idx_self2][0]} d:{t_dist_self[idx_self1,idx_self2]}, neg:{odata[1]}:{odata[2]}-{odata[3]}, im3:{odata[4][overlap_im_idx][0]}')
          assert False, 'neg tracklet does not overlap'
        # if self.debug:
        #   logging.info(f'mov:{sel_ndx}, tr1:{cur_dat[1]}:{cur_dat[2]}-{cur_dat[3]} im1:{cur_dat[4][idx_self1][0]} im2:{cur_dat[4][idx_self2][0]} d:{t_dist_self[idx_self1,idx_self2]}, neg:{odata[1]}:{odata[2]}-{odata[3]}, im3:{odata[4][overlap_im_idx][0]}')
        info.append([sel_ndx,curidx,idx_self1,idx_self2, overlap_tgt,overlap_im_idx])
        curims.append(np.stack([im1, im2, overlap_im], 0))

      curims = np.array(curims)
      info = np.array(info)
      curims = curims.reshape((-1,) + curims.shape[2:])
      curims = process_id_ims(curims, confd, distort, rescale)
      curims = curims.astype('float32')
      yield curims, info

def process_id_ims_par(im_arr,conf,distort,rescale):
  res_arr = []
  for ims in im_arr:
    res_arr.append(process_id_ims(ims,conf,distort,rescale))
  return res_arr

def process_id_ims(curims, conf, distort, rescale):
  """
  Applies preprocessing to the images
  """
  if curims.shape[3] == 1:
    curims = np.tile(curims, [1, 1, 1, 3])
  dummy_locs = np.ones([curims.shape[0],2,2]) * curims.shape[1]/2
  zz, _ = PoseTools.preprocess_ims(curims, dummy_locs, conf, distort, rescale)
  zz = zz.transpose([0, 3, 1, 2])
  zz = zz / 255.
  im_mean = np.array([[[0.485]], [[0.456]], [[0.406]]])
  im_std = np.array([[[0.229]], [[0.224]], [[0.225]]])
  zz = zz - im_mean
  zz = zz / im_std
  return zz

def read_ims_par(trx, trk_info, mov_file, conf,n_ex=50):
  '''
  Read images in parallel because otherwise it is really slow particularly for avis
  :param trx:
  :type trx:
  :param trk_info:
  :type trk_info:
  :param mov_file:
  :type mov_file:
  :param conf:
  :type conf:
  :param n_ex:
  :type n_ex:
  :return:
  :rtype:
  '''

  n_trk = len(trk_info)
  if n_trk < mp.cpu_count():
    n_threads = n_trk
  else:
    bytes_per_trk = n_ex*conf.imsz[0]*conf.imsz[1]*3
    max_pkl_bytes = 1024*1024*1024
    n_trk_per_thrd = max_pkl_bytes//bytes_per_trk
    n_threads = int(np.ceil(n_trk/n_trk_per_thrd))
    n_threads = max( mp.cpu_count(),n_threads)

  with mp.get_context('spawn').Pool(n_threads) as pool:

    trk_info_split = split_parallel(trk_info,n_threads)
    data = pool.starmap(read_tracklet_ims, [(trx, trk_info_split[n], mov_file, conf, n_ex, np.random.randint(100000)) for n in range(n_threads)])

  data = merge_parallel(data)

  return data

def read_data_files(data_files):
  data = []
  for curf in data_files:
    data.append(PoseTools.pickle_load(curf))
    os.remove(curf)

  data = merge_parallel(data)
  return data


def read_tracklet_ims(trx, trk_info, mov_file, conf, n_ex,seed):
  '''
  Read n_ex number of random images from tracklets specified in trk_info. The number of the images that can be returned is limited by pickle to 2GB. So saving the images to temp file and returning the file. Uses existing code that extracts animal images based on trx
  :param trx:
  :type trx:
  :param trk_info:
  :type trk_info:
  :param mov_file:
  :type mov_file:
  :param conf:
  :type conf:
  :param n_ex:
  :type n_ex:
  :param seed:
  :type seed:
  :return:
  :rtype:
  '''

  # Very important to set the seed as otherwise same set of images would be returned
  np.random.seed(seed)
  cap = movies.Movie(mov_file)

  all_ims = []
  for cur_trk in trk_info:
    rand_frs = []
    while len(rand_frs) < n_ex:
      cur_fr = np.random.choice(np.arange(cur_trk[1], cur_trk[2]+1))
      if np.isnan(trx[cur_trk[0]]['x'][0,cur_fr-cur_trk[1]]):
        continue
      rand_frs.append(cur_fr)

    cur_list = [[fr, cur_trk[0]] for fr in rand_frs]

    # Use trx based image patch generator
    ims = apt.create_batch_ims(cur_list, conf, cap, False, trx, None, use_bsize=False)
    all_ims.append([ims, cur_trk[0],cur_trk[1],cur_trk[2],cur_list])

  # tfile = tempfile.mkstemp()[1]
  # with open(tfile,'wb') as f:
  #   pickle.dump(all_ims,f)
  # cap.close()
  return all_ims

def split_parallel(x,n_threads,is_numpy=False):
  '''
  Splits an array to be used for multithreading
  :param x:
  :type x:
  :param n_threads:
  :type n_threads:
  :return:
  :rtype:
  '''
  nx = len(x)
  split = [range((nx * n) // n_threads, (nx * (n + 1)) // n_threads) for n in range(n_threads)]
  if is_numpy:
    split_x = tuple( x[split[n]] for n in range(n_threads))
  else:
    split_x = tuple( tuple(x[s] for s in split[n]) for n in range(n_threads))
  assert sum([len(curx) for curx in split_x]) == nx, 'Splitting failed'
  return split_x

def merge_parallel(data):
  data = [i for sublist in data for i in sublist]
  return data

def tracklet_pred(ims, net, conf, rescale):
    '''
    Do prediction over a set of images (typically generated using read_tracklet_ims
    :param ims:
    :type ims:
    :param net:
    :type net:
    :param conf:
    :type conf:
    :param rescale:
    :type rescale:
    :return:
    :rtype:
    '''
    preds = []
    n_threads = min(24, mp.cpu_count())
    n_batches = max(1,len(ims)//(3*n_threads))
    n_tr = len(ims)
    with mp.get_context('spawn').Pool(n_threads) as pool:
      processed_ims = pool.starmap(process_id_ims_par, [(ims[n:n+1],conf,False,rescale) for n in range(len(ims))])
      processed_ims = merge_parallel(processed_ims)
      for ix in range(len(processed_ims)):
          zz = processed_ims[ix]
          zz = zz.astype('float32')
          zz = torch.tensor(zz).cuda()
          with torch.no_grad():
              oo = net(zz).cpu().numpy()
          preds.append(oo)

      # for curb in range(n_batches):
      #   cur_set = ims[(curb*n_tr)//n_batches:( (curb+1)*n_tr)//n_batches]
      #   split_set = split_parallel(cur_set,n_threads)
      #   processed_ims = pool.starmap(process_id_ims_par, [(split_set[n],conf,False,rescale) for n in range(n_threads)])
      #   processed_ims = merge_parallel(processed_ims)
      #   for ix in range(len(processed_ims)):
      #       zz = processed_ims[ix]
      #       zz = zz.astype('float32')
      #       zz = torch.tensor(zz).cuda()
      #       with torch.no_grad():
      #           oo = net(zz).cpu().numpy()
      #       preds.append(oo)

    rr = np.array(preds)
    return rr

def compute_mining_data(net, data, trk_data, rescale, confd):
  '''
  Computes the distance between overlapping tracklets.
  :param net:
  :type net:
  :param data:
  :type data:
  :param trk_data:
  :type trk_data:
  :param rescale:
  :type rescale:
  :param confd:
  :type confd:
  :return:
  :rtype:
  '''
  ss_t, ee_t, _ = trk_data
  ims = [dd[0] for dd in data]
  n_tr = len(data)
  a = time.time()
  t_preds = tracklet_pred(ims, net, confd, rescale)
  b = time.time()
  # print(f'Time Taken to process images {b-a}')
  # n_threads = min(24, mp.cpu_count())
  # with mp.get_context('spawn').Pool(n_threads) as pool:
  #   x_split = []
  #   for n in range(n_threads):
  #     x_split.append(list(range((n_tr*n)//n_threads,(n_tr*(n+1))//n_threads)))
  #   dists = pool.starmap(compute_dists, [ (t_preds,ss_t,ee_t,x_split[n]) for n in n_threads])
  #
  # dists = [i for sublist in dists for i in sublist]
  dists = compute_dists(t_preds,ss_t,ee_t,range(n_tr))
  c = time.time()
  # print(f'Time taken to compute dists {c-b}')
  assert [d[-1] for d in dists] == list(range(n_tr))
  overlap_dist_mean = np.array([d[3] for d in dists])
  self_dist_mean = np.array([d[2] for d in dists])
  return dists, overlap_dist_mean, self_dist_mean

def compute_dists(t_preds, ss_t, ee_t, all_xx):
  """
  Computes the distance between the embeddings for the images. Two distances are computed -- 1) self dist: This is the distance between the images that belong to the same tracklet 2) overlap dist: This is the distance between the images of a tracklet to all the other trajectories that overlap with it in time.
  :param t_preds:
  :type t_preds:
  :param ss_t: start frames of the trajectories
  :type ss_t:
  :param ee_t: end frames of the trajectories
  :type ee_t:
  :param all_xx: trajectories to compute the distance for
  :type all_xx: list of int
  :return:
  :rtype:
  """

  dists = []
  for xx in all_xx:
    overlap_tgts, overlap_amt = get_overlap(ss_t, ee_t, ss_t[xx], ee_t[xx], xx)
    if overlap_tgts.size > 0:
      overlap_dist = np.linalg.norm(t_preds[xx:xx + 1, :, None] - t_preds[overlap_tgts, None], axis=-1)
      overlap_mean = np.mean(overlap_dist*overlap_amt[:,None,None])
      # overlap_mean is large for a tracklet if its iamges are far from other tracklets (not that useful for training), and small if images are close to iamges of ovrlapping tracklets (useful for training)
    else:
      overlap_dist = []
      overlap_mean = 2.

    self_dist = np.linalg.norm(t_preds[xx, :, None] - t_preds[xx, None], axis=-1)
    self_mean = np.mean(self_dist)
    # self_mean is small for a tracklet if all images are close to each other (not that useful for training), and large if iamges are far from each other (useful for training)
    dists.append([self_dist, overlap_dist, self_mean, overlap_mean, overlap_tgts, overlap_amt, xx])
  return dists

def load_id_wts(id_wts):
  net = get_id_net()
  cpt = torch.load(id_wts)
  net.load_state_dict(cpt['model_state_params'])
  return net.cuda()

def get_id_net():
  # model to use. we embed the animal images into 32 dim space
  net = models.resnet.resnet18(pretrained=True)
  net.fc = torch.nn.Linear(in_features=512, out_features=32, bias=True)

  net = net.cuda()
  return net

def train_id_classifier(all_data, conf, trks, save=False,save_file=None, bsz=16):
  """
  Trains the identity classifier/embedder
  :param all_data:
  :type all_data:
  :param conf:
  :type conf:
  :param trks:
  :type trks:
  :param save:
  :type save:
  :param save_file:
  :type save_file:
  :param bsz:
  :type bsz:
  :return:
  :rtype:
  """

  class ContrastiveLoss(torch.nn.Module):
    """
    Contrastive loss function.
    Based on: http://yann.lecun.com/exdb/publis/pdf/hadsell-chopra-lecun-06.pdf
    """

    def __init__(self, margin=2.0):
      super(ContrastiveLoss, self).__init__()
      self.margin = margin

    def forward(self, output1, output2, label):
      euclidean_distance = F.pairwise_distance(output1, output2, keepdim=True)
      loss_contrastive = torch.sum((1 - label) * torch.pow(euclidean_distance, 2) + (label) * torch.pow(
        torch.clamp(self.margin - euclidean_distance, min=0.0), 2))

      return loss_contrastive


  loss_history = []

  net = get_id_net()
  criterion = ContrastiveLoss()
  optimizer = optim.Adam(net.parameters(), lr=0.0001)

  # Create a new conf object so that we can use the posetools preprocessing function. However, we need to change the augmentation parameters and cropping parameters that are appropriate for the cropped images

  confd = copy.deepcopy(conf)
  if confd.trx_align_theta:
    confd.rrange = 10.
  else:
    confd.rrange = 180.
  confd.trange = min(conf.imsz) / 15
  # no flipping business for id
  confd.horz_flip = False
  confd.vert_flip = False
  confd.scale_factor_range = 1.1
  confd.brange = [-0.05, 0.05]
  confd.crange = [0.95, 1.05]
  rescale = conf.link_id_rescale
  n_iters = conf.link_id_training_iters

  # how many times to sample. Actually it ends up being one less than specified
  num_times_sample = conf.link_id_mining_steps
  sampling_period = round(n_iters / num_times_sample)
  debug = conf.get('link_id_debug',False)

  logging.info('Training ID network ...')
  net.eval()
  net = net.cuda()

  # Set mining distances to identical dummy values initially
  trk_data = []
  mining_dists = []
  for data, trk in zip(all_data,trks):
    ss, ee = trk.get_startendframes()
    tgt_id = np.array([r[1] for r in data])
    ss_t = ss[tgt_id]
    ee_t = ee[tgt_id]
    trk_data.append([ss_t,ee_t,tgt_id])
    t_dist = None
    n_tr = len(data)
    self_dist = np.ones(n_tr)
    overlap_dist = np.ones(n_tr)
    mining_dists.append([t_dist,overlap_dist, self_dist])

  # Create the dataset and dataloaders. Again seed is important!
  distort = True
  train_dset = id_dset(all_data, mining_dists, trk_data, confd, rescale, valid=False, distort=distort, debug=debug)
  n_workers = 10 if not debug else 0
  train_loader = torch.utils.data.DataLoader(train_dset, batch_size=bsz, pin_memory=True, num_workers=n_workers,worker_init_fn=lambda id: np.random.seed(id))
  train_iter = iter(train_loader)

  # Save example training images for debugging.
  ex_ims, ex_info = next(train_iter)
  ex_ims = ex_ims.numpy()
  im_save_file = os.path.splitext(save_file)[0]+'_ims.mat'
  hdf5storage.savemat(im_save_file,{'example_ims':ex_ims})
  logging.info(f'Saved sampled ID training images to {im_save_file}')

  for epoch in tqdm(range(n_iters)):

    if epoch % sampling_period == 0 and epoch > 0:
      # compute the mining data and recreate datasets and dataloaders with updated mining data
      net = net.eval()
      mining_dists = []
      for data, cur_trk_data in zip(all_data,trk_data):
        cur_dists = compute_mining_data(net, data, cur_trk_data, rescale, confd)
        mining_dists.append(cur_dists)

      # net = net.train()
      net =net.eval()
      del train_iter, train_loader, train_dset
      train_dset =  id_dset(all_data,mining_dists,trk_data,confd,rescale,valid=True, distort=distort, debug=debug)
      train_loader = torch.utils.data.DataLoader(train_dset,batch_size=bsz,pin_memory=True,num_workers=10,worker_init_fn=lambda id: np.random.seed(id*epoch))
      train_iter = iter(train_loader)


    curims, data_info = next(train_iter)
    curims = curims.cuda()
    curims = curims.reshape((-1,)+ curims.shape[2:])
    optimizer.zero_grad()
    output = net(curims)
    output = output.reshape((-1,3) + output.shape[1:])
    output1, output2, output3 = output[:,0], output[:,1], output[:,2]
    # output1, output2 are from the same tracklet so they should be close. output3 should be far from both output1 and output2

    l1 = criterion(output1, output2, 0)
    l2 = criterion(output1, output3, 1)
    l3 = criterion(output2, output3, 1)
    loss_contrastive = l1 + l2 + l3
    loss_contrastive.backward()
    optimizer.step()
    if epoch%5000==0 and save and save_file is not None and epoch>0:
      wt_out_file = f'{save_file}-{epoch}.p'
      torch.save({'model_state_params': net.state_dict(),'loss_history':loss_history}, wt_out_file)

    loss_history.append(loss_contrastive.item())

  wt_out_file = f'{save_file}'
  torch.save({'model_state_params': net.state_dict(), 'loss_history': loss_history}, wt_out_file)

  del train_iter, train_loader, train_dset
  return net, loss_history


def link_trklet_id(linked_trks, net, mov_files, conf, all_trx, n_per_trk=50,rescale=1, min_len_select=5, debug=False, keep_all_preds=False):
  '''
  Links the pure tracklets using identity

  :param linked_trks: pure linked tracks
  :param net: id network
  :param mov_files: movie files
  :param conf: poseconfig object
  :param all_trx: pure linked tracks loaded in the trx format for apt.create_batch_ims
  :param n_per_trk: number of samples to use per trk to designate an id.
  :param rescale:
  :param min_len_select:
  :param debug:
  :return: list of id linked tracklets
  '''

  thresh_perc = 5 # percentile to use for thresholds

  net.eval()
  all_data = []
  preds = None
  maxn_all = []
  pred_map = []
  # pred_map keeps track of which sample belongs to which trajectory


  # sample images for each tracklet and then find the embeddings for them
  for ndx in range(len(linked_trks)):
    # Sample images from the tracklets
    trk = linked_trks[ndx]
    mov_file = mov_files[ndx]
    trx = all_trx[ndx]
    ss, ee = trk.get_startendframes()
    maxn_all.append(max(ee))

    # For each tracklet chose n_per_trk random examples and the find their embedding. Ignore short tracklets
    sel_tgt = np.where((ee-ss+1)>=min_len_select)[0]
    sel_ss = ss[sel_tgt]; sel_ee = ee[sel_tgt]
    trk_info = list(zip(sel_tgt, sel_ss, sel_ee))
    logging.info(f'Sampling images from {len(sel_ss)} tracklets to assign identity to the tracklets ...')
    start_t = time.time()
    cur_data = read_ims_par(trx, trk_info, mov_file, conf, n_ex=n_per_trk)
    end_t = time.time()
    logging.info(f'Sampling images took {round((end_t-start_t)/60)} minutes')

    merge_data = []
    merge_tgt_id = []
    s_sz = 200
    # find ceil
    n_split = int(np.ceil(len(cur_data)/s_sz))
    for idx in tqdm(range(n_split)):

      # data = read_data_files([curf])
      ids1 = s_sz*idx
      ids2 = min(s_sz*(idx+1), len(cur_data))
      data = cur_data[ids1:ids2]
      tgt_id = np.array([r[1] for r in data])
      merge_tgt_id.extend(tgt_id.tolist())

      if debug:
        merge_data.extend(data)

      # pred_map keeps track of which sample belongs to which trajectory
      ims = []
      for curndx in range(len(data)):
        curims = data[curndx][0]
        ims.append(curims)
        pred_map.append([ndx, tgt_id[curndx]])

      # Find the embeddings for the images
      cur_preds = tracklet_pred(ims, net, conf, rescale)

      if cur_preds.size>0:
        if preds is None:
          preds = cur_preds
        else:
          preds = np.concatenate([preds, cur_preds],axis=0)

    merge_tgt_id = np.array(merge_tgt_id)
    cur_d = [merge_data, sel_tgt, merge_tgt_id, ss, ee, sel_ss, sel_ee]
    all_data.append(cur_d)

  pred_map = np.array(pred_map)

  maxcosts_all = []
  params = get_default_params(conf)
  link_costs_arr = []
  for tndx in range(len(linked_trks)):
    maxcost_missed = estimate_maxcost_missed(linked_trks[tndx], params)
    maxcost = estimate_maxcost(linked_trks[tndx], params)
    maxcosts_all.append(np.array([maxcost, ] + maxcost_missed.tolist()))
    # FInd the linkinking costs between the tracklets
    st, en = linked_trks[tndx].get_startendframes()
    link_costs = get_link_costs(linked_trks[tndx], st, en, params)
    link_costs_arr.append(link_costs)


  minv, maxv = linked_trks[0].get_min_max_val()
  minv = np.min(minv, axis=0)
  maxv = np.max(maxv, axis=0)
  bignumber = np.sum(maxv - minv) * 2000

  # Cluster the embedding using linkage. each group in groups specifies which tracklets belong to the same animal
  logging.info('Stitching tracklets based on identity ...')
  t_info = [d[3:5] for d in all_data]

  dist_mat = get_id_dist_mat(preds)
  diag_mat = np.diag(dist_mat)


  # Find thresholds for close and far

  # use intra tracklet distance to find the close threshold
  close_thresh = np.percentile(diag_mat,100-thresh_perc)
  close_thresh = max(0.5,close_thresh)

  # use distance to overlapping tracklets to find the far thresholds. Using just the first movie for now
  mov1_sel = pred_map[:,0]==0
  sel = pred_map[mov1_sel, 1]
  st_sel = all_data[0][-2]
  en_sel = all_data[0][-1]
  ns = sel.size

  overlap = np.zeros([ns,ns])
  for ndx in range(ns):
    aa, bb = get_overlap(st_sel, en_sel, st_sel[ndx], en_sel[ndx], ndx)
    overlap[ndx, aa] = bb
  mov1_dist = dist_mat[np.ix_(mov1_sel,mov1_sel)]
  overlap_dist = mov1_dist[overlap>0.1]
  far_thresh = np.percentile(overlap_dist,thresh_perc)


  n_tr = dist_mat.shape[0]
  dist_mat[range(n_tr),range(n_tr)] = 0.

  pred_map_orig = pred_map.copy()
  rem_id_trks = np.zeros(dist_mat.shape[0]) < 0.5
  groups = []
  groups_only_id = []
  used_trks = []


  # create id clusters iteratively by first finding the largest cluster and then adding the missing links. Most of the codes dirtiness is for keeping track of the id tracks and other tracks that have been used till now
  while True:
    rem_id_idx = np.where(rem_id_trks)[0]
    if len(rem_id_idx)==0:
      break
    elif len(rem_id_idx)==1:
      gr = rem_id_idx
    else:
      dist_mat_cur = dist_mat[rem_id_trks, :][:,rem_id_trks]
      pred_map_cluster = pred_map_orig[rem_id_trks]
      gr_cur = get_largest_cluster(dist_mat_cur, close_thresh, t_info, pred_map_cluster)
      far_ids = dist_mat_cur[gr_cur].mean(axis=0) > far_thresh
      far_ids = rem_id_idx[far_ids]
      gr = rem_id_idx[gr_cur]

    gr = gr.tolist()
    groups_only_id.append(gr.copy())

    # Ignore the tracklets that have been used already for the next round.
    for mov_ndx in range(len(linked_trks)):
      ids_ignore = []
      for ff in far_ids:
        if pred_map_orig[ff][0] == mov_ndx:
          ids_ignore.append(pred_map_orig[ff][1])
      for uu in used_trks:
        if pred_map[uu][0] == mov_ndx:
          ids_ignore.append(pred_map[uu][1])

      gr, pred_map = add_missing_links(linked_trks, [gr], conf, pred_map, mov_ndx, ids_ignore, maxcosts_all[mov_ndx],maxn_all[mov_ndx], bignumber, link_costs_arr)
      gr = gr[0]

    for gg in gr:
      if gg<len(rem_id_trks):
        rem_id_trks[gg] = False

    used_trks.extend(gr)
    groups.append(gr)

  # If we want to keep all the predictions, then we need to add the remaining tracklets to the groups
  if keep_all_preds:
    for mov_ndx in range(len(linked_trks)):
      cur_used_ids = [pred_map[uu][1] for uu in used_trks if pred_map[uu][0] == mov_ndx]
      for ids in range(linked_trks[mov_ndx].ntargets):
        if ids not in cur_used_ids:
          pred_map = np.concatenate([pred_map, [[mov_ndx, ids]]], axis=0)
          groups.append([len(pred_map)-1])


  # Old style grouping
  # groups_new = groups.copy()
  # pred_map_id = pred_map.copy()
  # groups = cluster_tracklets_id(preds, pred_map_orig, t_info, conf.link_maxframes_delete)
  #
  # id_groups = groups.copy()
  # pred_map = pred_map_orig.copy()
  # for mov_ndx in range(len(linked_trks)):
  #   groups, pred_map = add_missing_links(linked_trks, groups, conf, pred_map, mov_ndx, None, maxcosts_all[mov_ndx],maxn_all[mov_ndx],bignumber)


  # Link the actual pose data. ids is TrkFile.Tracklet instance that keeps track about which tracklet in which frame belongs to which animal

  ids = []
  for trk, data in zip(linked_trks,all_data):
    ss, ee = data[3:5]
    cur_id = TrkFile.Tracklet(defaultval=-1, size=(1, trk.ntargets,trk.T))
    cur_id.allocate( (1,), ss-trk.T0, ee-trk.T0)
    ids.append(cur_id)

  for ndx, gr in enumerate(groups):
    for gg in gr:
      mov_ndx, trk_ndx = pred_map[gg]
      cur_id = ids[mov_ndx]
      cur_trk = linked_trks[mov_ndx]
      data = all_data[mov_ndx]
      sf,ef = data[3:5]
      cur_p = np.ones(ef[trk_ndx]-sf[trk_ndx]+1)* ndx
      cur_id.settarget(cur_p, trk_ndx, sf[trk_ndx] -cur_trk.T0, ef[trk_ndx]-cur_trk.T0)

  #   cur_tgt = min(sel_tgt[gr])
  #   for gg in gr:
  #     if sel_tgt[gg] == cur_tgt: continue
  #     match_tgt = sel_tgt[gg]
  #     trk.pTrk[..., ss[match_tgt]:ee[match_tgt]+1, cur_tgt] = trk.pTrk[..., ss[match_tgt]:ee[match_tgt]+1, match_tgt]
  #     to_remove.append(match_tgt)
  #     assigned_ids[match_tgt] = cur_tgt
  #
  # # Delete the trks that have been merged
  # trk.pTrk = np.delete(trk.pTrk, to_remove, -1)
  # for k in trk.trkFields:
  #   if trk.__dict__[k] is not None:
  #     trk.__dict__[k] = np.delete(trk.__dict__[k], to_remove, -1)
  # trk.ntargets = trk.ntargets - len(to_remove)

  logging.info(f'Deleting short trajectories with length less than {conf.link_maxframes_delete}')

  params = get_default_params(conf)
  for cur_id, cur_trk in zip(ids, linked_trks):
    _, maxv = cur_id.get_min_max_val()
    nids = np.max(maxv) + 1
    t0s = np.zeros(nids, dtype=int)
    t1s = np.zeros(nids, dtype=int)
    ids_remove = []
    for id in range(nids):
      idx = cur_id.where(id)
      # idx = np.nonzero(id==ids)
      if idx[1].size>0:
        t0s[id] = np.min(idx[1])
        t1s[id] = np.max(idx[1])
      else:
        t1s[id] = -1
        ids_remove.append(id)
    isdummy = TrkFile.Tracklet(defaultval=False, size=(1, nids, cur_id.T))
    isdummy.allocate((1,), t0s, t1s)

    cur_id, ids_short = delete_short(cur_id, isdummy, params)
    if len(linked_trks)<2:
      _, cur_id = cur_id.unique()

    ids_left = [i for i in range(nids) if (i not in ids_short) and (i not in ids_remove)]
    # Apply the ids to trk.
    cur_trk.apply_ids(cur_id)
    cur_trk.pTrkiTgt = np.array(ids_left)
    interpolate_gaps(cur_trk)

  return linked_trks


def interpolate_gaps(trk):
  '''
  Fill in small gaps using interpolation
  :param trk:
  :return:
  '''

  thresh = 0.5
  # If the radio of (distance of the pose across the gap) and
  # (the size of the animal) is less than this thresh than interpolate

  max_gap = 3
  ss,ee = trk.get_startendframes()
  for xx in range(trk.ntargets):
      pp = trk.gettargetframe(xx,np.arange(ss[xx],ee[xx]+1))
      jj = pp[0, 0, :, 0]
      qq = np.ones(jj.shape[0]+2)>0
      qq[1:-1] = np.isnan(jj)

      qq1 = np.where(   qq[:-2]   & (~qq[1:-1]) )[0]
      qq2 = np.where( (~qq[1:-1]) &   qq[2:] )[0]
      rr = qq1[1:]-qq2[:-1]-1
      for ndx in range(rr.size):
          a1 = pp[:,:,qq1[ndx+1],0]
          a2 = pp[:,:,qq2[ndx],0]
          dd = a1-a2
          dd = np.linalg.norm(dd,axis=1).mean()
          dd = dd/rr[ndx] # amortize the distance by the gap size
          sz1 = a1.max(axis=0)-a1.min(axis=0)
          sz1 = np.mean(sz1)
          sz2 = a2.max(axis=0)-a2.min(axis=0)
          sz2 = np.mean(sz2)
          csz = (sz1+sz2)/2

          if (rr[ndx]<= max_gap) and (dd/csz < thresh):
            for dim in range(pp.shape[1]):
              ii = PoseTools.linspacev(a1[:,dim],a2[:,dim],rr[ndx]+2)

              pp[:,dim,qq2[ndx]+1:qq1[ndx+1],0] = ii[:,1:-1]
      trk.settarget(pp[...,0],xx,ss[xx],ee[xx])


def get_dist(p1, p2):
  return np.sum(np.abs(p1 - p2), axis=(0, 1)) / p2.shape[0]


def add_missing_links(linked_trks, groups, conf, pred_map, tndx, ignore_idx, maxcosts_all, maxn, bignumber, link_costs_arr):

  params = get_default_params(conf)
  mult = 3
  max_link = 15*params['maxframes_missed']

  st, en = linked_trks[tndx].get_startendframes()
  link_costs = link_costs_arr[tndx]

  occ = np.ones([len(groups), maxn], 'int') * -1
  for ndx, gr in enumerate(groups):
    for gg in gr:
      mov_ndx, trk_ndx = pred_map[gg]
      if mov_ndx == tndx:
        occ[ndx, st[trk_ndx]:en[trk_ndx] + 1] = trk_ndx


  # FInd all the breaks for the identity groups
  breaks = []
  for ndx in range(occ.shape[0]):
    qq = np.zeros(maxn + 2) < 1
    qq[1:-1] = occ[ndx] > -0.5

    qq1 = np.where(qq[:-2] & (~qq[1:-1]))[0]
    qq2 = np.where((~qq[1:-1]) & qq[2:])[0]
    to_rem = (qq1==0) | (qq2==maxn-1)
    qq1 = qq1[~to_rem]
    qq2 = qq2[~to_rem]
    breaks.append(np.array(list(zip(qq1, qq2))))

  if ignore_idx is None:
    taken = [pred_map[g,1] for gr in groups for g in gr if pred_map[g,0]==tndx]
  else:
    taken = ignore_idx

  linked_groups = groups.copy()
  new_pred_map = []
  # MOst of the crappiness of the code is keeping track of what has been taken and what has not been
  for ndx in range(len(linked_groups)):
    for bx in range(breaks[ndx].shape[0]):
      b_st, b_en = breaks[ndx][bx, :]

      if (b_st > 0) & (b_en<maxn) &( (b_en-b_st)<=max_link):
        st_trk = occ[ndx][b_st - 1]
        en_trk = occ[ndx][b_en + 1]
        trk2add = find_path(link_costs, st_trk, en_trk, b_en + 1, st, en, maxcosts_all, taken, mult, bignumber)
        for tid in trk2add:
          match_pred = np.where((pred_map==[tndx,tid]).all(axis=1))[0]
          if len(match_pred)>0:
            linked_groups[ndx].append(match_pred[0])
          else:
            new_pred_map.append([tndx,tid])
            linked_groups[ndx].append(len(pred_map)+len(new_pred_map)-1)
          taken.append(tid)

  if len(new_pred_map)>0:
    new_pred_map = np.concatenate([pred_map, new_pred_map],axis=0)
  else:
    new_pred_map = pred_map
  return linked_groups, new_pred_map

##

def find_path(link_costs, st_idx, en_idx, en_fr,st, en, maxcosts_all,taken, mult, bignumber):
  '''

  :param link_costs: cost of linking current tracklet to other tracklets
  :param st_idx: tracklet id to start linking from
  :param en_idx: tracklet id to link to
  :param en_fr: end frame
  :param st: all the tracklet starts
  :param en: all the tracklet ends
  :param maxcosts_all: linking costs across missing frames
  :param taken: do no use these tracklets
  :param mult:
  :param bignumber:
  :return: path for linking as a list of tracklets

  This function uses the shorteset path algorithm. Cost of linking tracklets tr1 that ends at t and tr2 that starts at t+n is maxcosts_all[n-1]/2. This is because we only want to mildly penalize the missing frame, as otherwise the algorithm will try to fill in some other tracklet tr3 that could be far from both tr1 and tr2.
  '''

  link_st = en[st_idx] + 1
  link_en = st[en_idx] - 1
  taken_arr = np.zeros(len(st)) > 0.5
  taken_arr[taken] = True

  # int_trks are the intermediate tracklets that lie with the gap and are not taken
  int_trks = np.where( (st>=link_st) & (en<= link_en) &
~taken_arr)[0]
  n_trks = len(int_trks)

  # edge mat will be used to compute the shortest path. [:,0] corresponds to starting tracklet and [:,-1] correspoinds to ending tracklet
  edge_mat = np.ones([n_trks+2,n_trks+2])*bignumber*(link_en-link_st+1)

  def get_link_cost(cur_link):
    cost = cur_link[1]
    n_miss = int(cur_link[2])
    if cost > mult*maxcosts_all[n_miss]:
      connect = False
    else:
      connect = True

    if n_miss>0:
      cur_cost = cost * (n_miss + 1) * maxcosts_all[0] / maxcosts_all[n_miss] * 1.5
    else:
      cur_cost = cost

    return connect, cur_cost

  if link_costs[st_idx][1].size > 0:
    en_ix = np.where(link_costs[st_idx][1][:,0]==en_idx)[0]
    if en_ix.size>0:
      connect, cost =  get_link_cost(link_costs[st_idx][1][en_ix[0]])
      edge_mat[0,-1] =  cost

  # Costs from start tracklet
  for ix in range(link_costs[st_idx][1].shape[0]):
    lix = int(link_costs[st_idx][1][ix, 0])
    if lix not in int_trks: continue
    lix_id = np.where(int_trks==lix)[0][0]
    connect, cost = get_link_cost(link_costs[st_idx][1][ix])
    if connect:
        edge_mat[0,lix_id+1] = cost

  # Costs to end tracklet
  for ix in range(link_costs[en_idx][0].shape[0]):
    lix = int(link_costs[en_idx][0][ix, 0])
    if lix not in int_trks: continue
    lix_id = np.where(int_trks==lix)[0][0]
    connect, cost = get_link_cost(link_costs[en_idx][0][ix])
    if connect:
        edge_mat[lix_id+1,-1] = cost

  # For all other tracklets
  for ix1 in range(n_trks):
    cur_trk = int_trks[ix1]
    for ix in range(link_costs[cur_trk][1].shape[0]):
      lix = int(link_costs[cur_trk][1][ix, 0])
      if lix not in int_trks: continue
      lix_id = np.where(int_trks==lix)[0][0]
      connect, cost = get_link_cost(link_costs[cur_trk][1][ix])
      if connect:
        edge_mat[ix1+1,lix_id + 1] = cost

  dmat, conn_mat = scipy.sparse.csgraph.shortest_path(edge_mat,return_predecessors=True,indices=0)

  path = []

  if dmat[-1]>=bignumber:
    # This happens if thre is no path. In this case connect as much as possible to the st_idx tracklet and en_idx tracklet
    broken = True
    possible_end = np.where(dmat[1:-1]<bignumber)[0]
    if possible_end.size > 0:
      possible_end_trks = int_trks[possible_end]
      lengths = en[possible_end_trks] - en[st_idx]
      max_length_idx = np.argmax(lengths)
      p_end = possible_end[max_length_idx] + 1
      path.append(p_end)
    else:
      p_end = 0
  else:
    broken = False
    p_end = n_trks + 1

  p_start = 0
  while p_end!=p_start:
    p_end = conn_mat[p_end]
    path.append(p_end)

  # remove the first tracklet which corresponds to st_idx
  path = path[:-1] if len(path)>0 else path

  if broken:
    # connect the longest possible path to en_idx
    dmat_e, conn_mat = scipy.sparse.csgraph.shortest_path(edge_mat.T, return_predecessors=True, indices=-1)
    possible_st = np.where(dmat_e[1:-1]<bignumber)[0]
    if possible_st.size > 0:
      possible_st_trks = int_trks[possible_st]
      lengths =  st[en_idx] - st[possible_st_trks]
      max_length_idx = np.argmax(lengths)
      p_start = possible_st[max_length_idx] + 1
      p_end = n_trks+1

      while p_start!=p_end:
        path.append(p_start)
        p_start = conn_mat[p_start]

  # reconstruct the path in terms of tracklets
  path = [int_trks[ix-1] for ix in path]

  return path


def get_link_costs(tt, st, en, params):
  maxn = max(en)
  n_missed = params['maxcost_framesfit']
  link_costs = []
  for curt in range(tt.ntargets):

    cur_st = st[curt]
    cur_en = en[curt]
    start_matches = []
    p_cur = tt.gettargetframe(curt, cur_st)[..., 0]
    for tends in range(n_missed + 1):
      stfr = cur_st - tends - 1
      if stfr < 0:
        continue
      trk_sts = np.where(en == stfr)[0]
      for idx in trk_sts:
        p_en = tt.gettargetframe(idx, stfr)[..., 0]
        cur_cost = get_dist(p_cur, p_en)[0]
        start_matches.append([idx, cur_cost, tends])
    start_matches = np.array(start_matches)

    end_matches = []
    p_cur = tt.gettargetframe(curt, cur_en)[..., 0]
    for tends in range(n_missed + 1):
      enfr = cur_en + tends + 1
      if enfr > maxn:
        continue
      trk_ends = np.where(st == enfr)[0]
      for idx in trk_ends:
        p_st = tt.gettargetframe(idx, enfr)[..., 0]
        cur_cost = get_dist(p_cur, p_st)[0]
        end_matches.append([idx, cur_cost, tends])
    end_matches = np.array(end_matches)
    link_costs.append([start_matches, end_matches])
  return link_costs

def embed_dist(xx,yy):
  ddm = np.zeros([xx.shape[0],yy.shape[0]])
  for ix in range(xx.shape[0]):
   ddm[ix, :] = np.median(np.linalg.norm(xx[ix:ix+1] - yy, axis=-1), axis=(1, 2))
  return ddm

def get_id_dist_mat(embed):
  n_threads = min(24, mp.cpu_count())
  with mp.get_context('spawn').Pool(n_threads) as pool:
    split_set = split_parallel(embed[:,:,None], n_threads, is_numpy=True)
    processed_dist = pool.starmap(embed_dist, [(split_set[n], embed[:,None]) for n in range(n_threads)])
    processed_dist = merge_parallel(processed_dist)
    processed_dist =np.array(processed_dist)
  return processed_dist

def get_largest_cluster(dist_mat, thresh, t_info, pred_map):
  distArray = ssd.squareform( dist_mat)
  Z = linkage(distArray, 'average')
  # plt.figure()
  # dn = dendrogram(Z)

  F = fcluster(Z, thresh, criterion='distance')

  groups = []
  n_fr = [max(sel[1]) for sel in t_info]
  tr_len = []
  for mov_ndx, trk_ndx in pred_map:
    cur_len = t_info[mov_ndx][1][trk_ndx] - t_info[mov_ndx][0][trk_ndx] + 1
    tr_len.append(cur_len)
  tr_len = np.array(tr_len)

  g_len = np.array([tr_len[F==(i+1)].sum() for i in range(max(F))])
  largest_cluster = np.argmax(g_len)
  sel_idx = np.where(F==(largest_cluster+1))[0]
  cur_group = []
  extra_groups = []
  ctline = [np.zeros(n) for n in n_fr]
  # ctline keeps track of the frames that are already part of the current group.
  for cc in sel_idx:
    mov_ndx, trk_ndx = pred_map[cc]
    sel_ss = t_info[mov_ndx][0][trk_ndx]
    sel_ee = t_info[mov_ndx][1][trk_ndx]
    prev_overlap = np.sum(ctline[mov_ndx][sel_ss:sel_ee + 1]) / (sel_ee - sel_ss + 1)
    if prev_overlap < 0.05:
      cur_group.append(cc)
      ctline[mov_ndx][sel_ss:sel_ee + 1] += 1

  return cur_group



def cluster_tracklets_id(embed, pred_map, t_info, min_len):

  n_tr = embed.shape[0]
  n_ex = embed.shape[1]
  ddm = get_id_dist_mat(embed)
  # ddm = np.ones([n_tr, n_tr]) * np.nan
  #
  # for xx in tqdm(range(n_tr)):
  #   ddm[xx, :] = np.median(np.linalg.norm(embed[xx, None, :, None] - embed[:, None, :], axis=-1), axis=(1,2))
  # plt.figure(); plt.imshow(ddm)


  ddm[range(n_tr), range(n_tr)] = 0.
  distArray = ssd.squareform( ddm)
  Z = linkage(distArray, 'average')
  # plt.figure()
  # dn = dendrogram(Z)

  ##
  thres = 1.
  F = fcluster(Z, thres, criterion='distance')

  groups = []
  n_fr = [max(sel[1]) for sel in t_info]
  tr_len = []
  for mov_ndx, trk_ndx in pred_map:
    cur_len = t_info[mov_ndx][1][trk_ndx] - t_info[mov_ndx][0][trk_ndx] + 1
    tr_len.append(cur_len)
  tr_len = np.array(tr_len)

  g_len = np.array([tr_len[F==(i+1)].sum() for i in range(max(F))])
  f_order = np.argsort(g_len)[::-1]

  # Create groups in order of their sizes. While creating groups ensure two tracklets that have significant overlap in time are not part of the same group.

  for ndx in f_order:
    cur_gr = np.where(np.array(F) == (ndx + 1))[0]
    cur_gr_ord = np.argsort(-tr_len[cur_gr])
    cur_gr = cur_gr[cur_gr_ord]

    cur_group = []
    extra_groups = []
    ctline = [np.zeros(n) for n in n_fr]
    # ctline keeps track of the frames that are already part of the current group.
    for cc in cur_gr:
      mov_ndx, trk_ndx = pred_map[cc]
      sel_ss = t_info[mov_ndx][0][trk_ndx]
      sel_ee = t_info[mov_ndx][1][trk_ndx]
      prev_overlap = np.sum(ctline[mov_ndx][sel_ss:sel_ee+1])/(sel_ee-sel_ss+1)
      if prev_overlap>0.05:
        if (sel_ee-sel_ss+1)>min_len:
          extra_groups.append([cc])
      else:
        cur_group.append(cc)
        ctline[mov_ndx][sel_ss:sel_ee+1] +=1

    tot_len = sum([ct.sum() for ct in ctline])
    if tot_len>min_len:
      groups.append(cur_group)
    groups.extend(extra_groups)

  return groups


def test_assign_ids_data():
  """
  test_assign_ids_data:
  loads data from a trkfile and runs assign_ids, stitch, delete_short, and unique on them
  :return:
  """
  
  matplotlib.use('TkAgg')
  plt.ion()
  
  trkfile = '/groups/branson/home/kabram/temp/roian_multi/200918_m170234vocpb_m170234_odor_m170232_f0180322_full_min2.trk.part'
  outtrkfile = '/groups/branson/bransonlab/apt/tmp/200918_m170234vocpb_m170234_odor_m170232_f0180322_full_min2_kbstitched_tracklet.trk'
  
  #trkfile = '/groups/branson/home/kabram/temp/roian_multi/200918_m170234vocpb_m170234_odor_m170232_f0180322_full1.trk.part'
  #outtrkfile = '/groups/branson/bransonlab/apt/tmp/200918_m170234vocpb_m170234_odor_m170232_f0180322_full1_kbstitched_v2.trk'
  
  # parameters
  params = {}
  params['verbose'] = 1
  params['maxframes_missed'] = 10
  params['maxframes_delete'] = 10
  params['maxcost_prctile'] = 95.
  params['maxcost_mult'] = 1.25
  params['maxcost_framesfit'] = 3
  params['maxcost_heuristic'] = 'secondorder'
  nframes_test = np.inf
  
  showanimation = False
  
  trk = TrkFile.Trk(trkfile)
  T = np.minimum(np.inf, trk.T)
  # p should be d x nlandmarks x maxnanimals x T, while pTrk is nlandmarks x d x T x maxnanimals
  # p = np.transpose(trk['pTrk'],(1,0,3,2))
  nframes_test = int(np.minimum(T, nframes_test))
  params['maxcost'] = estimate_maxcost(trk, prctile=params['maxcost_prctile'], mult=params['maxcost_mult'],heuristic=params['maxcost_heuristic'])
  params['maxcost_missed'] = estimate_maxcost_missed(trk, params['maxcost_framesfit'],prctile=params['maxcost_prctile'], mult=params['maxcost_mult'],heuristic=params['maxcost_heuristic'])
  logging.info('maxcost set to %f' % params['maxcost'])
  logging.info('maxcost_missed set to ' + str(params['maxcost_missed']))
  ids, costs = assign_ids(trk, params, T=nframes_test)
  if isinstance(ids, np.ndarray):
    nids_original = np.max(ids)+1
  else:
    _, nids_original = ids.get_min_max_val()
    nids_original = nids_original+1
  
  ids, isdummy = stitch(trk, ids, params)
  ids, ids_short = delete_short(ids, isdummy, params)
  _, ids = ids.unique()
  trk.apply_ids(ids)
  
  # save to file
  trk.save(outtrkfile)
  # TrkFile.save_trk(outtrkfile,newtrk)
  
  plt.figure()
  nids = trk.ntargets
  # nids = newtrk['pTrk'].shape[3]
  logging.info('%d ids in %d frames, removed %d ids' % (nids, nframes_test, nids_original-nids))
  nidsplot = int(np.minimum(nids, np.inf))
  minp, maxp = trk.get_min_max_val()
  minp = np.min(minp)
  maxp = np.max(maxp)
  startframes, endframes = trk.get_startendframes()
  
  hax = []
  for d in range(trk.d):
    hax.append(plt.subplot(1, trk.d, d+1))
    hax[d].set_title('coord %d' % d)
  
  for id in range(nidsplot):
    
    logging.info('Target %d, %d frames (%d to %d)' % (id, endframes[id]-startframes[id]+1, startframes[id], endframes[id]))
    
    ts = np.arange(startframes[id], endframes[id]+1, dtype=int)
    n = ts.size
    p = trk.gettargetframe(id, ts).reshape((trk.nlandmarks, trk.d, n))
    mu = np.nanmean(p, axis=0)
    idxnan = np.where(np.all(np.isnan(mu), axis=0))[0]
    for d in range(trk.d):
      h, = hax[d].plot(ts, mu[d, :], '.-')
      if d == 0:
        color = h.get_color()
      hax[d].plot(ts[0], mu[d, 0], 'o', color=color, mfc=color)
      hax[d].plot(ts[-1], mu[d, -1], 's', color=color, mfc=color)
      if idxnan.size > 0:
        hax[d].plot(ts[idxnan], np.zeros(idxnan.size), 'x', color=color)
  plt.show(block=True)
  
  if showanimation:
    
    colors = mixed_colormap(nids)
    colors[:, :4] *= .75
    plt.figure()
    h = [None, ] * nids
    htrail = [None, ] * nids
    hax = plt.gca()
    hax.set_ylim((minp, maxp))
    hax.set_xlim((minp, maxp))
    traillen = 50
    trail = np.zeros((trk.d, traillen, trk.ntargets))
    trail[:] = np.nan
    plt.show(block=False)
    
    T0 = np.nanmin(startframes)
    for t in range(T0, np.nanmax(endframes)+1):
      p = trk.getframe(t)
      isrealidx = trk.real_idx(p).flatten()
      mu = np.nanmean(p, axis=0).reshape((trk.d, trk.ntargets))
      off = t-T0
      if off < traillen:
        trail[:, off, :] = mu
      else:
        trail = np.append(trail[:, 1:, :], mu.reshape((trk.d, 1, nids)), axis=1)
      for id in range(nids):
        if t > endframes[id] or t < startframes[id]:
          if htrail[id] is not None:
            htrail[id].remove()
            htrail[id] = None
        else:
          if htrail[id] is None:
            htrail[id], = plt.plot(trail[0, :, id], trail[1, :, id], '-', color=colors[id, :] * .5+np.ones(4) * .5)
          else:
            htrail[id].set_data(trail[0, :, id], trail[1, :, id])
      
      for id in np.where(isrealidx)[0]:
        if h[id] is None:
          h[id], = plt.plot(p[:, 0, :, id].flatten(), p[:, 1, :, id].flatten(), '.-', color=colors[id, :])
        else:
          h[id].set_data(p[:, 0, :, id].flatten(), p[:, 1, :, id].flatten())
      for id in np.where(~isrealidx)[0]:
        if h[id] is not None:
          h[id].remove()
          h[id] = None
      plt.pause(.01)


def test_estimate_maxcost():
  
  matplotlib.use('TkAgg')
  plt.ion()
  
  trkfile = '/groups/branson/home/kabram/temp/roian_multi/200918_m170234vocpb_m170234_odor_m170232_f0180322_full1.trk.part'
  
  # parameters
  params = {}
  params['verbose'] = 1
  params['maxframes_missed'] = 10
  params['maxframes_delete'] = 10
  params['maxcost_prctile'] = 95.
  params['maxcost_mult'] = 1.25
  params['maxcost_framesfit'] = 3
  
  trk = TrkFile.Trk(trkfile=trkfile)
  # frames should be consecutive
  # assert np.all(np.diff(trk['pTrkFrm'], axis=1) == 1), 'pTrkFrm should be consecutive frames'
  # p should be d x nlandmarks x maxnanimals x T, while pTrk is nlandmarks x d x T x maxnanimals
  # p = np.transpose(trk['pTrk'], (1, 0, 3, 2))
  
  maxcost0 = estimate_maxcost(trk, prctile=params['maxcost_prctile'], mult=params['maxcost_mult'])
  maxcost1 = estimate_maxcost_missed(trk, params['maxcost_framesfit'],
                                     prctile=params['maxcost_prctile'], mult=params['maxcost_mult'])
  maxcost = np.append(np.atleast_1d(maxcost0), maxcost1.flatten())
  
  plt.figure()
  plt.plot(np.arange(maxcost.size)+1, maxcost, 'o-')
  plt.show(block=True)
  
def test_recognize_ids():
  
  matplotlib.use('tkAgg')
  plt.ion()
  
  # locations of data
  expdir = '/groups/branson/bransonlab/apt/experiments/data/200918_m170234vocpb_m170234_odor_m170232_f0180322_allframes'
  rawtrkfile = os.path.join(expdir,'rawtrk.trk')
  outtrkfile = os.path.join(expdir,'kbstiched_debug.trk')

  trxfile = os.path.join(expdir,'trx.mat')
  dell2ellfile = os.path.join(expdir,'perframe/dell2ell.mat')
  moviefile = os.path.join(expdir,'movie.mjpg')
  movieidxfile = os.path.join(expdir,'index.txt')

  # landmarks to use for calculating a centroid to compare to motr tracking
  bodylandmarks = np.array([0,1])
  plotlandmarkorder = np.array([0,2,3,0,1])

  # max ell2ell distance for motr tracking to be considered close
  distthresh = 10
  # fill holes of size < close_diameter
  close_diameter = 5
  # whether to use a normalized version of idcosts such that total costs sum to 1 for each prediction
  # I think this makes more sense, but it makes setting parameters harder...
  normalize_idcosts = True

  # debugging - how many frames to test
  nframes_test = np.inf

  # big number, means don't assign here
  BIGCOST = 100000.
  
  # whether to plot an animation of tracking results
  showanimation = False

  # whether to plot
  plot_debug_input = False
  
  # parameters for matching
  params = {}
  # printing debugging output
  params['verbose'] = 1
  # weight of the movement cost (weight of j cost is 1)
  if normalize_idcosts:
    params['weight_movement'] = 1./100.
  else:
    params['weight_movement'] = 1.
  # cost of setting a target to not be detected
  params['cost_missing'] = 50.*params['weight_movement']
  # cost of having a prediction that is not assigned to a target
  params['cost_extra'] = 50.*params['weight_movement']
  # if a target is not detected, we use its last know location for matching.
  # if it's been more than maxframes_missed frames since last location, then
  # don't use this past location in assigning ids
  params['maxframes_missed'] = 20
  
  # load in unlinked data
  trk = TrkFile.Trk(trkfile=rawtrkfile)
  if not trk.issparse:
    trk.convert2sparse()
  npts = trk.size[0]
  d = trk.size[1]
  
  # load in motr tracking
  trx = TrkFile.load_trx(trxfile)
  # load in pre-computed dell2ell data
  dell2ell = TrkFile.load_perframedata(dell2ellfile)

  # frames tracked by APT
  T0,T1 = trk.get_frame_range()
  T1 = int(np.minimum(T1,T0+nframes_test-1))
  T = T1-T0+1
  ntargets = trk.ntargets

  # there are 2 targets, so we only need one of dell2ell
  assert len(trx['x']) == 2
  dell2ell = dell2ell[0][T0-trx['startframes'][0]:T1-trx['startframes'][0]+1]
  isclose = dell2ell <= distthresh
  if close_diameter > 0:
    se_close = np.ones(close_diameter)
    isclose = scipy.ndimage.morphology.binary_closing(isclose,se_close)

  # APT issue where same prediction returned twice sometimes -- remove duplicates
  ndup = 0
  for t in range(T0,T1+1):
    x = trk.getframe(t)
    D = np.sum(np.abs(x.reshape((npts*d,1,ntargets))-x.reshape((npts*d,ntargets,1))),axis=0)
    D[np.tril_indices(ntargets,k=0)] = np.inf
    (i,j) = np.where(D<=.1)
    if i.size > 0:
      x[:,:,:,j] = trk.defaultval
      trk.setframe(x,t)
      ndup += 1
  print('%d duplicate values found and removed'%ndup)

  if plot_debug_input:
    # plot trx and trk info to make sure they line up in time
    plt.figure()
    t = T0
    p = trk.getframe(t)
    for i in range(trk.ntargets):
      plt.plot(p[:,0,0,i],p[:,1,0,i],'r.')
      if (t >= trx['startframes'][i]) and (t <= trx['endframes'][i]):
        plt.plot(trx['x'][i][t-trx['startframes'][i]],trx['y'][i][t-trx['startframes'][i]],'o')
    ax = plt.gca()
    ax.set_aspect('equal')

    # plot dell2ell
    plt.figure()
    plt.plot(np.where(isclose)[0]-trx['startframes'][0]+T0,dell2ell[isclose],'.')
    plt.plot(np.where(~isclose)[0]-trx['startframes'][0]+T0,dell2ell[~isclose],'.')
    plt.legend(['close','far'])
    plt.xlabel('Frame')
    plt.ylabel('ell2ell distance (mm)')
  
  # compute motr-based assignment costs
  idcosts = [None,]*T
  for t in range(T0,T1+1):
    i = t - T0
    pcurr = trk.getframe(t)
    idxreal = trk.real_idx(pcurr)
    pcurr = pcurr[:,:,idxreal]
    npred = pcurr.shape[2]
    if isclose[i]:
      idcosts[i] = np.zeros((ntargets,npred))
      continue
    center = np.reshape(np.mean(pcurr[bodylandmarks,:,:],axis=0),[2,1,npred])
    trxpos = np.zeros((2,ntargets,1))
    trxpos[:] = np.nan
    ntrxcurr = 0
    for j in range(ntargets):
      if (t >= trx['startframes'][j]) and (t <= trx['endframes'][j]):
        trxpos[0, j, 0] = trx['x'][j][t-trx['startframes'][j]]
        trxpos[1, j, 0] = trx['y'][j][t-trx['startframes'][j]]
        ntrxcurr+=1
    D = np.sqrt(np.sum(np.square(center-trxpos),axis=0)) # ntargets x npred
    badidx = np.isnan(D)
    if normalize_idcosts:
      z = np.nansum(D,axis=0)
      z[z<=0.] = 1.
      D = D/z
    D[badidx] = BIGCOST
    idcosts[i] = D

  if plot_debug_input:
    maxnpred = trk.ntargets
    y = np.zeros((T,maxnpred))
    y[:] = np.nan
    for t in range(T0,T1+1):
      i = t - T0
      y[i,:idcosts[i].shape[1]] = idcosts[i][0,:]-idcosts[i][1,:]
  
    fig, ax = plt.subplots(3, 1, sharex=True, sharey=False)
    for i in range(maxnpred):
      ax[0].plot(y[:,i],'.',label='Prediction %d'%i)
    
    for i in range(len(trx['x'])):
      t0 = np.maximum(T0,trx['startframes'][i])
      t1 = np.minimum(T1,trx['endframes'][i])
      ax[1].plot(np.arange(t0-T0,t1+1-T0),trx['x'][i][t0-trx['startframes'][i]:t1+1-trx['startframes'][i]],'x',label='Motr target %d'%i)
      ax[2].plot(np.arange(t0-T0,t1+1-T0),trx['y'][i][t0-trx['startframes'][i]:t1+1-trx['startframes'][i]],'x',label='Motr target %d'%i)
    
    for i in range(maxnpred):
      p = trk.gettargetframe(i,np.arange(T0,T1+1,dtype=int))
      center = np.mean(p[bodylandmarks,:,:,:],axis=0)
      ax[1].plot(center[0,:,0],'.',label='Prediction %d'%i)
      ax[2].plot(center[1,:,0],'.',label='Prediction %d'%i)
  
      
    ax[0].title.set_text('j cost difference')
    ax[1].title.set_text('x-coordinate')
    ax[2].title.set_text('y-coordinate')
    ax[0].legend(loc='upper right')
    ax[1].legend(loc='upper right')
    ax[2].legend(loc='upper right')
  
  # compute id assignment
  nframes_test = int(np.minimum(T, nframes_test))
  ids, costs, stats = assign_recognize_ids(trk, idcosts, params, T=nframes_test)
  
  # apply to tracking
  trk.apply_ids(ids)

  # save linked tracking to output
  trk.save(outtrkfile)

  # plot some visualization of the results
  idxextra = np.where(stats['nextra']>0)[0]
  idxmissing = np.where(stats['nmissing']>0)[0]
  idxboth = np.where(np.logical_and(stats['nmissing']>0,stats['nextra']>0))[0]
  
  trk0 = TrkFile.Trk(trkfile=rawtrkfile)
  if not trk0.issparse:
    trk0.convert2sparse()
  
  fig, ax = plt.subplots(trk.ntargets+1, 1, sharex=True, sharey=False)
  ax[0].plot(costs,'.',label='Total cost',zorder=10)
  ax[0].plot(stats['cost_movement'],'.',label='Movement cost',zorder=20)
  ax[0].plot(np.where(~isclose)[0],stats['cost_id'][~isclose],'.',label='Id cost, not close',zorder=30)
  ax[0].title.set_text('Cost')

  for i in range(trk0.ntargets):
    t0 = np.maximum(T0,trx['startframes'][i])
    t1 = np.minimum(T1,trx['endframes'][i])
    p0 = trk0.gettargetframe(i,np.arange(t0,t1+1,dtype=int))
    center0 = np.mean(p0[bodylandmarks,:,:,:],axis=0)
    ax[1].plot(center0[0,:,0],'o',label='Raw %d'%i,zorder=5)
    ax[2].plot(center0[1,:,0],'o',label='Raw %d'%i,zorder=5)
  
  for i in range(trk.ntargets):
    t0 = np.maximum(T0,trx['startframes'][i])
    t1 = np.minimum(T1,trx['endframes'][i])
    p = trk.gettargetframe(i,np.arange(t0,t1+1,dtype=int))
    center = np.mean(p[bodylandmarks,:,:,:],axis=0)
    trxx = trx['x'][i][t0-trx['startframes'][i]:t1+1-trx['startframes'][i]]
    trxy = trx['y'][i][t0-trx['startframes'][i]:t1+1-trx['startframes'][i]]
    if T <= 2000: #plotting slow
      ax[1].plot(np.tile(np.arange(t0-T0,t1+1-T0).reshape(1,t1-t0+1),(trk.ntargets,1)),np.concatenate((trxx.reshape((1,t1-t0+1)),center[0,:,:].reshape(1,t1-t0+1)),axis=0),'k.-')
      ax[2].plot(np.tile(np.arange(t0-T0,t1+1-T0).reshape(1,t1-t0+1),(trk.ntargets,1)),np.concatenate((trxy.reshape((1,t1-t0+1)),center[1,:,:].reshape(1,t1-t0+1)),axis=0),'k.-')
    ax[1].plot(np.arange(t0-T0,t1+1-T0),trxx,'+-',label='Motr %d'%i,zorder=10)
    ax[2].plot(np.arange(t0-T0,t1+1-T0),trxy,'+-',label='Motr %d'%i,zorder=10)

    ax[1].plot(center[0,:,0],'.-',label='Linked %d'%i,zorder=20)
    ax[2].plot(center[1,:,0],'.-',label='Linked %d'%i,zorder=20)
  
  ax[1].title.set_text('x-coordinate')
  ax[2].title.set_text('y-coordinate')
  for i in range(3):
    box = ax[i].get_position()
    ax[i].set_position([box.x0, box.y0, box.width * 0.8, box.height])
    # Put a legend to the right of the current axis
    #if i > 0:
    ax[i].legend(loc='center left', bbox_to_anchor=(1, 0.5))
    ylim = np.array(ax[i].get_ylim()).reshape((2,1))
    ax[i].plot(np.tile(idxextra,(2,1)),np.tile(ylim,(1,idxextra.size)),'c-',zorder=0)
    ax[i].plot(np.tile(idxmissing,(2,1)),np.tile(ylim,(1,idxmissing.size)),'m-',zorder=0)
    ax[i].plot(np.tile(idxboth,(2,1)),np.tile(ylim,(1,idxboth.size)),'k-',zorder=0)
    ax[i].set_ylim(ylim)

  
  plt.show(block=True)
  if showanimation:
    minp, maxp = trk.get_min_max_val()
    minp = np.min(minp)
    maxp = np.max(maxp)
    
    colors = mixed_colormap(ntargets)
    colors[:, :4] *= .75
    plt.figure()
    h = [None, ] * ntargets
    htrail = [None, ] * ntargets
    hax = plt.gca()
    hax.set_ylim((minp, maxp))
    hax.set_xlim((minp, maxp))
    traillen = 50
    trail = np.zeros((trk.d, traillen, trk.ntargets))
    trail[:] = np.nan
    sf,ef = trk.get_startendframes()
    
    for t in range(T0, T1+1):
      p = trk.getframe(t)
      isrealidx = trk.real_idx(p).flatten()
      mu = np.nanmean(p, axis=0).reshape((trk.d, trk.ntargets))
      off = t-T0
      if off < traillen:
        trail[:, off, :] = mu
      else:
        trail = np.append(trail[:, 1:, :], mu.reshape((trk.d, 1, ntargets)), axis=1)
      for j in range(ntargets):
        if t > ef[j] or t < sf[j]:
          if htrail[j] is not None:
            htrail[j].remove()
            htrail[j] = None
        else:
          if htrail[j] is None:
            htrail[j], = plt.plot(trail[0, :, j], trail[1, :, j], '-', color=colors[j, :] * .5+np.ones(4) * .5)
          else:
            htrail[j].set_data(trail[0, :, j], trail[1, :, j])
      
      for j in np.where(isrealidx)[0]:
        if h[j] is None:
          h[j], = plt.plot(p[plotlandmarkorder, 0, :, j].flatten(), p[plotlandmarkorder, 1, :, j].flatten(), '.-', color=colors[j, :])
        else:
          h[j].set_data(p[plotlandmarkorder, 0, :, j].flatten(), p[plotlandmarkorder, 1, :, j].flatten())
      for j in np.where(~isrealidx)[0]:
        if h[j] is not None:
          h[j].remove()
          h[j] = None
      hax.title.set_text('Frame %d'%t)
      plt.pause(.001)
  
  
  print('finished')
  

if __name__ == '__main__':
  # test_match_frame()
  # test_assign_ids_data()
  test_recognize_ids()
  # test_estimate_maxcost()
  # test_assign_ids()
