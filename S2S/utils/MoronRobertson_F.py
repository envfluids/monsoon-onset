import xarray as xr
import numpy as np
import pandas as pd

def sequence_overlap(X, lseason, nday):
    nr, nv = np.shape(X)
    nyear = nr // lseason
    indice = []
    for i in range(nday):
        row = np.arange(i, lseason + i)
        indice.append(row)
    indice = np.array(indice)
    nseq, lseq = np.shape(indice)
    Y = np.zeros((lseq * nyear, nv * nday))
    for i in range(nyear):
        sample = X[i * lseason: (i + 1) * lseason]
        sample = np.vstack([np.tile(sample[0], (nday - 1, 1)), sample])
        sample1 = np.zeros((lseq, nday * nv))
        for j in range(nday):
            sample1[:lseq, (j * nv):(j + 1) * nv] = sample[indice[j], :nv]
        Y[(i * lseq) : (i + 1) *lseq,:nv*nday] = sample1
    return Y


def onset_agro_bis(X, lseason, defdry, sw, wet, sd, dry, window):
    # Setup Section
    N, C = np.shape(X)
    nyear = N // lseason
    W = np.zeros(np.shape(X))
    W[X > defdry] = 1
    
    swet = None # Have to add this line for python
    if sw > 1:
        swet = sequence_overlap(np.transpose([np.arange(lseason)]), lseason, sw)
        swet = np.transpose(swet[sw - 1:lseason,:])
        swet = (swet.reshape((-1, 1), order='F') @ np.ones((1, C))) + np.ones(((lseason - (sw - 1)) * sw, 1)) @ (np.arange(0, lseason * C, lseason).reshape(1, -1))
        swet = swet.reshape((sw,C*(lseason-(sw-1))), order='F')
                
    sdry = None #Have to add this line for python
    if sd > 1:
        sdry = sequence_overlap(np.transpose([np.arange(lseason)]), lseason, sd)
        sdry = np.transpose(sdry[sd - 1:lseason,:])
        sdry = (sdry.reshape((-1, 1), order='F') @ np.ones((1, C))) + np.ones(((lseason - (sd - 1)) * sd, 1)) @ (np.arange(0, lseason * C, lseason).reshape(1, -1))
        sdry = sdry.reshape((sd,C*(lseason-(sd-1))), order='F')
    
    O1 = np.full((nyear, C), np.nan)
    O2 = np.full((nyear, C), np.nan)
    
    S = window - (sd - 1)
    S2 = sequence_overlap(np.transpose([np.arange(lseason)]), lseason, S)
    S2 = np.transpose(S2[S - 1:lseason])
    Lw = lseason - (sw - 1)
    
    # Calculation of MWmean
    SWmean = np.zeros((nyear * Lw, C))
    for i in range(nyear):
        sample = X[(i * lseason): ((i + 1) * lseason), :]
        sample_flat = sample.ravel(order="F")
        if sw > 1:
            SWmean[(i * Lw):(Lw * (i + 1)),:] = (np.reshape(np.sum(sample_flat[swet.astype(int)], axis=0), 
                                                            (lseason - (sw - 1), C), order="F"))
        else:
            SWmean[(i * Lw):(Lw * (i + 1)),:] = sample
            
    MWmean = np.zeros(C)
    for i in range(C):
        MWmean[i] = np.mean(SWmean[SWmean[:,i] > defdry, i]) 
    if wet == 0:
        wet = MWmean
    else:
        wet = wet * np.ones((1, C))
        #MWmean = wet
    
    # Calculation of O1 and O2
    for i in range(nyear):
        sample = X[(i * lseason): ((i + 1) * lseason), :]
        wsample = W[(i * lseason): ((i + 1) * lseason), :]
        sample_flat = sample.ravel(order="F")
        SW = sample
        SD = sample
        if sw > 1:
            SW = np.reshape(np.sum(sample_flat[swet.astype(int)], axis=0), (lseason - (sw - 1), C), order= "F")
        if sd > 1:
            SD = np.reshape(np.sum(sample_flat[sdry.astype(int)], axis=0), (lseason - (sd - 1), C), order = "F")
        nrw, ncw = np.shape(SW)
        nrd, ncd = np.shape(SD)
        for j in range(C):
            SW_extension = np.concatenate([SW[:,j], np.ones(sw - 1) * SW[lseason - sw, j]])
            SD_extension = np.concatenate([SD[:,j], np.zeros(sd - 1)])
            tab = np.column_stack([sample[:, j], wsample[:,j], SW_extension, SD_extension])
            nrtab, nctab = np.shape(tab)
            o1 = np.where((tab[:, 2] >= wet[j]) & (tab[:, 1] == 1))[0]
            D = tab[:, 3]
            D = np.transpose(D[S2.astype(int)])
            D = np.vstack([D, np.zeros((window - sd, S))])
            if o1.size > 0:
                O1[i, j] = o1[0]
                tab2 = D[o1, :]
                o2 = o1[np.min(tab2, axis = 1) > dry]
                if o2.size > 0:
                    O2[i, j] = o2[0]
    return O1, O2, MWmean


