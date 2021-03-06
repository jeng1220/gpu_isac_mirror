#!/home/schoenf/applications/sphire/v1.1/envs/sphire_1.3/bin/python
# coding: latin-1

"""
Author: Pawel A.Penczek, 09/09/2006 (Pawel.A.Penczek@uth.tmc.edu)
Copyright (c) 2000-2006 The University of Texas - Houston Medical School

This software is issued under a joint BSD/GNU license. You may use the source
code in this file under either license. However, note that the complete EMAN2
and SPARX software packages have some GPL dependencies, so you are responsible
for compliance with the licenses of these packages if you opt to use BSD 
licensing. The warranty disclaimer below holds in either instance.

This complete copyright notice must be included in any revised version of the
source code. Additional authorship citations may be added, but existing author
citations must be preserved.

This program is free software; you can redistribute it and/or modifyit under
the terms of the GNU General Public License as published by the Free Software
Foundation; either version 2 of the License, or (at your option) any later 
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program; if not, write to the Free Software Foundation, Inc., 59 Temple
Place, Suite 330, Boston, MA  02111-1307 USA
"""

#====================================================================[ import ]

# compatibility
from __future__ import print_function

# system
import os
import sys
import time

import ctypes
import numpy as np


from EMAN2 import EMNumPy
from EMAN2 import EMUtil

import sparx as spx

import random
import applications

from future import standard_library
standard_library.install_aliases()
from builtins import range

from logger import Logger, BaseLogger_Files

import global_def
from global_def   import EMData, Util, ERROR, Transform

from applications import  ali2d_base, ali2d_base_gpu_isac_CLEAN, MPI_start_end, within_group_refinement, within_group_refinement_CLEAN

from isac import *

from   optparse import OptionParser, SUPPRESS_HELP
import configparser
from inspect import currentframe, getframeinfo

from random       import randint, seed, jumpahead

from alignment    import Numrinit, ringwe, search_range
from filter       import filt_tanl, filt_ctf
from fundamentals import rot_shift2D, fshift, fft, resample
from pixel_error  import multi_align_stability
from statistics   import ave_series

import subprocess
from logger import Logger, BaseLogger_Files

from statistics   import hist_list

from EMAN2db import db_open_dict

import mpi
mpi.mpi_init(0, [])

global GPU_ISAC_VERSION
GPU_ISAC_VERSION = "2.3.2 (dev)"

#=================================================================[ gpu setup ]

global GPU_CLASS_LIMIT
global GPU_DEVICES
global MPI_GPU_COMM

GPU_CLASS_LIMIT, GPU_DEVICES = 100, []

#=================================================================[ Blockdata ]

"""
Blockdata holds all the administrative information about running ISAC. This
includes:

    Added in header:
    - nproc: number of processes available to MPI (size of MPI_COMM_WORLD)
    - myid: mpi rank of this process
    - main_node: mpi rank of the mpi "main" process (traditionally node_0)
    - shared_comm: communicator to other mpi nodes that share the same memory
    - myid_on_node: mpi id of this process within shared_comms ie, the same node
    - no_of_processes_per_group: number of processes in this process shared_comms group

    Added in main():
    - stack: path to particle images to be run through ISAC
    - masterdir: path to ISAC master directory
    - stack_ali2d: path to .bdb file holding the alignment parameters (this file exists only once)
    - total_nima: total number of images in the stack (see above)
    - 2dalignment: path to the results of the pre-alignment

    Added in create_zero_group():
    - subgroup_comm: mpi communicator for all processes with local rank zero
    - subgroup_size: size of the zero group
    - subgroup_myid: local process id within the zero group
"""

global Blockdata
Blockdata = {}
Blockdata["nproc"]        = mpi.mpi_comm_size(mpi.MPI_COMM_WORLD)

Blockdata["myid"]         = mpi.mpi_comm_rank(mpi.MPI_COMM_WORLD)
Blockdata["main_node"]    = 0
Blockdata["shared_comm"]  = mpi.mpi_comm_split_type(mpi.MPI_COMM_WORLD, mpi.MPI_COMM_TYPE_SHARED, 0, mpi.MPI_INFO_NULL)
Blockdata["myid_on_node"] = mpi.mpi_comm_rank(Blockdata["shared_comm"])
Blockdata["no_of_processes_per_group"] = mpi.mpi_comm_size(Blockdata["shared_comm"])

masters_from_groups_vs_everything_else_comm = ( mpi.mpi_comm_split(mpi.MPI_COMM_WORLD, Blockdata["main_node"] == Blockdata["myid_on_node"], Blockdata["myid_on_node"]) )
Blockdata["color"], Blockdata["no_of_groups"], balanced_processor_load_on_nodes = spx.get_colors_and_subsets( Blockdata["main_node"], 
                                                                                                              mpi.MPI_COMM_WORLD, Blockdata["myid"],
                                                                                                              Blockdata["shared_comm"], 
                                                                                                              Blockdata["myid_on_node"], 
                                                                                                              masters_from_groups_vs_everything_else_comm )
global_def.BATCH = True
global_def.MPI   = True

NAME_OF_JSON_STATE_FILE = "my_state.json"
NAME_OF_ORIGINAL_IMAGE_INDEX = "originalid"
NAME_OF_RUN_DIR  = "run"
NAME_OF_MAIN_DIR = "generation_"
DIR_DELIM = os.sep

# create an mpi subgroup containing all nodes whith local/node mpi id zero
def create_zero_group():

    if( Blockdata["myid_on_node"] == 0 ): 
        submyids = [ Blockdata["myid"] ]
    else:  
        submyids = []

    submyids = spx.wrap_mpi_gatherv( submyids, Blockdata["main_node"], mpi.MPI_COMM_WORLD )
    submyids = spx.wrap_mpi_bcast  ( submyids, Blockdata["main_node"], mpi.MPI_COMM_WORLD )
    
    world_group = mpi.mpi_comm_group( mpi.MPI_COMM_WORLD )
    subgroup    = mpi.mpi_group_incl( world_group, len(submyids), submyids )
    
    Blockdata["subgroup_comm"] = mpi.mpi_comm_create( mpi.MPI_COMM_WORLD, subgroup )
    mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

    Blockdata["subgroup_size"] = -1
    Blockdata["subgroup_myid"] = -1
    
    if( mpi.MPI_COMM_NULL != Blockdata["subgroup_comm"] ):
        Blockdata["subgroup_size"] = mpi.mpi_comm_size( Blockdata["subgroup_comm"] )
        Blockdata["subgroup_myid"] = mpi.mpi_comm_rank( Blockdata["subgroup_comm"] )
    
    mpi.mpi_barrier( mpi.MPI_COMM_WORLD )
    return

def checkitem( item, mpi_comm = -1 ):
    global Blockdata
    if mpi_comm == -1:  
        mpi_comm = mpi.MPI_COMM_WORLD
    if( Blockdata["myid"] == Blockdata["main_node"] ):
        if( os.path.exists(item) ): 
            isthere = True
        else: 
            isthere = False
    else: 
        isthere = False
    isthere = spx.bcast_number_to_all( isthere, source_node=Blockdata["main_node"], mpi_comm=mpi_comm )
    mpi.mpi_barrier( mpi_comm )
    return isthere

def mpi_assert( condition, msg ):
    if not condition:
        mpi_rank = mpi.mpi_comm_rank(mpi.MPI_COMM_WORLD)
        print( "MPI PROC["+str(mpi_rank)+"] ASSERTION ERROR:", msg, file=sys.stderr)
        sys.stderr.flush()
        mpi.mpi_finalize()
        sys.exit()

#=================================================================[ utilities ]

def print_splash():

    global GPU_ISAC_VERSION
    select = int(np.random.random()*6+1)
    print("")

    if select == 1:
        print( "          ____________________________________        ")
        print( "         |                                    |       ")
        print( "   ______|        Welcome to GPU ISAC!        |______ ")
        print( "   \     |" + " "*(18-len(GPU_ISAC_VERSION)/2) + GPU_ISAC_VERSION + \
               " "*(36-(18-len(GPU_ISAC_VERSION)/2+len(GPU_ISAC_VERSION)))+"|     / " )
        print( "    \    |____________________________________|    /  ")
        print( "    /             |                  |             \  ")
        print( "   /______________|                  |______________\ ")

    if select == 2:
        print( "     __________________ ____ ___  .___  _________   _____  _________  ")
        print( "    /  _____/\______   \    |   \ |   |/   _____/  /  _  \ \_   ___ \ ")
        print( "   /   \  ___ |     ___/    |   / |   |\_____  \  /  /_\  \/    \  \/ ")
        print( "   \    \_\  \|    |   |    |  /  |   |/        \/    |    \     \____")
        print( "    \______  /|____|   |______/   |___/_______  /\____|__  /\______  /")
        print( "           \/                                 \/         \/        \/ ")
        print( " " * (70-len(GPU_ISAC_VERSION)) + GPU_ISAC_VERSION )

    if select == 3:
        print( "      __________  __  __   _________ ___   ______")
        print( "     / ____/ __ \/ / / /  /  _/ ___//   | / ____/")
        print( "    / / __/ /_/ / / / /   / / \__ \/ /| |/ /     ")
        print( "   / /_/ / ____/ /_/ /  _/ / ___/ / ___ / /___   ")
        print( "   \____/_/    \____/  /___//____/_/  |_\____/   ")
        print( " " * (49-len(GPU_ISAC_VERSION)) + GPU_ISAC_VERSION )

    if select == 4:
        print( "    ██████╗ ██████╗ ██╗   ██╗    ██╗███████╗ █████╗  ██████╗")
        print( "   ██╔════╝ ██╔══██╗██║   ██║    ██║██╔════╝██╔══██╗██╔════╝")
        print( "   ██║  ███╗██████╔╝██║   ██║    ██║███████╗███████║██║     ")
        print( "   ██║   ██║██╔═══╝ ██║   ██║    ██║╚════██║██╔══██║██║     ")
        print( "   ╚██████╔╝██║     ╚██████╔╝    ██║███████║██║  ██║╚██████╗")
        print( "    ╚═════╝ ╚═╝      ╚═════╝     ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝")
        print( " " * (60-len(GPU_ISAC_VERSION)) + GPU_ISAC_VERSION )

    if select == 5:
        print( "     ▄████  ██▓███   █    ██     ██▓  ██████  ▄▄▄       ▄████▄  ")
        print( "    ██▒ ▀█▒▓██░  ██▒ ██  ▓██▒   ▓██▒▒██    ▒ ▒████▄    ▒██▀ ▀█  ")
        print( "   ▒██░▄▄▄░▓██░ ██▓▒▓██  ▒██░   ▒██▒░ ▓██▄   ▒██  ▀█▄  ▒▓█    ▄ ")
        print( "   ░▓█  ██▓▒██▄█▓▒ ▒▓▓█  ░██░   ░██░  ▒   ██▒░██▄▄▄▄██ ▒▓▓▄ ▄██▒")
        print( "   ░▒▓███▀▒▒██▒ ░  ░▒▒█████▓    ░██░▒██████▒▒ ▓█   ▓██▒▒ ▓███▀ ░")
        print( "    ░▒   ▒ ▒▓▒░ ░  ░░▒▓▒ ▒ ▒    ░▓  ▒ ▒▓▒ ▒ ░ ▒▒   ▓▒█░░ ░▒ ▒  ░")
        print( "     ░   ░ ░▒ ░     ░░▒░ ░ ░     ▒ ░░ ░▒  ░ ░  ▒   ▒▒ ░  ░  ▒   ")
        print( "   ░ ░   ░ ░░        ░░░ ░ ░     ▒ ░░  ░  ░    ░   ▒   ░        ")
        print( "         ░             ░         ░        ░        ░  ░░ ░      ")
        print( "                                                       ░        ")
        print( " " * (64-len(GPU_ISAC_VERSION)) + GPU_ISAC_VERSION )

    if select == 6:
        print( "    _____________________________________________________________________________ ")
        print( "   |                                                                             |")
        print( "   |           ______  _____  _     _      _____ _______ _______ ______          |")
        print( "   |          |  ____ |_____] |     |        |   |______ |_____| |               |")
        print( "   |          |_____| |       |_____|      __|__ ______| |     | |_____          |")
        print( "   |                                                                             |")
        print( "   |_____________________________________________________________________________|")
        print( " " * (82-len(GPU_ISAC_VERSION)) + GPU_ISAC_VERSION )

def print_progress( msg, progress, total):
    done = int(float(progress+1)/total*50.0)
    sys.stdout.write("\r["+msg+"]["+"="*done+"-"*(50-done)+"]~[%d/%d]~[%.2f]"%(progress+1, total, (float(progress+1)/total)*100.0))
    sys.stdout.flush()

def normalize_particle_image(
    aligned_image,
    shrink_ratio,
    target_radius,
    target_dim,
    align_params,
    filament_width=-1,
    ignore_helical_mask=False,
    show_progress="" ):
    """
    Function to normalize <aligned_image>. Note that the normalization also
    includes the shrinking/re-scaling of the particle images. The normalization
    itself is being done by subtracting the mean of the data inside a particle
    mask (signal) and dividing by the variance outsice of the mask (noise).
    NOTE: Image will first be shrunken/re-scaled in order to make the particle
    radius match between what ISAC uses for processing and the actual data.
    Afterwards the re-scaled image will be padded/cropped in order to match the
    internal image size used by ISAC.

    Args:
        aligned_image (EMData): EMData object holding image data.

        shrink_ratio (float): Ratio by which particles are to be re-scaled.

        target_radius (int): ISAC target radius.

        target_dim (int): ISAC target image size for processing (usually 76).

        align_params: Contains (pre-)alignment parameters; we apply the shifts.

        filament_width (int): Filament width when processing helical data. When
            a non-default value is provided this function assumes data to be
            filament images in which case a rectangular mask of the given width
            is applied to all particle images. [Default: -1]

        ignore_helical_mask (bool): Only relevant if filament_width is used. If
            set to False the data will be multiplied with the mask to remove all
            data/noise outside of the mask. If set to True the mask will still
            be used for the normalization but afterwards NOT multiplied with the
            particle images. [Default: False]

        show_progress (string): Print progress bar while processing. [Default: ""]
    """

    # particle image dimension after scaling/shrinking
    new_dim = int( aligned_image.get_xsize()*shrink_ratio + 0.5 )

    # create re-usable mask for non-helical particle images
    if filament_width == -1:
        if new_dim >= target_dim:
            mask = spx.model_circle( target_radius, target_dim, target_dim )
        else:
            mask = spx.model_circle( new_dim//2-2, new_dim, new_dim )

    # apply any available alignment parameters
    aligned_image = rot_shift2D( aligned_image, 0, align_params[1], align_params[2], 0 )

    # resample if necessary
    if shrink_ratio != 1.0:
        aligned_image = resample( aligned_image, shrink_ratio )

    # crop images if necessary
    if new_dim > target_dim:
        aligned_image = Util.window( aligned_image, target_dim, target_dim, 1 )

    current_dim = aligned_image.get_xsize()

    # create custom masks for filament particle images
    if filament_width != -1:
        mask = Util.model_rotated_rectangle2D( radius_long=int( np.sqrt(2*current_dim**2) )//2, # long  edge of the rectangular mask
                                                  radius_short=int( filament_width*shrink_ratio+0.5 )//2, # short edge of the rectangular mask
                                                  nx=current_dim, ny=current_dim, angle=aligned_image.get_attr("segment_angle") )

    # normalize using mean of the data and variance of the noise
    p = Util.infomask( aligned_image, mask, False )
    aligned_image -= p[0]
    p = Util.infomask( aligned_image, mask, True )
    aligned_image /= p[1]

    # optional: burn helical mask into particle images
    if filament_width != -1 and not ignore_helical_mask:
        aligned_image *= mask

    # pad images in case they have been shrunken below the target_dim
    if new_dim < target_dim:
        aligned_image = spx.pad( aligned_image, target_dim, target_dim, 1, 0.0 )

    # ship it
    return aligned_image

#======================================================================[ ISAC ]

def iter_isac_pap(
    alldata,
    ir, ou, rs,
    xr, yr, ts,
    maxit, CTF, snr, dst,
    FL, FH, FF,
    init_iter, iter_reali,
    stab_ali, thld_err,
    img_per_grp, generation,
    random_seed=None, new=False):
    """ 
    Core function to set up the next iteration of ISAC.

    Args:
        alldata (UNKNOWN TYPE): All image data [nima=len(alldata): no. of images]
        
        ir (int): Inner ring value (in pixels) of the resampling to polar 
            coordinates.
            [Default: 1]
        
        ou (int): Target particle radius used when ISAC processes the data.
            Images will be scaled to conform to this value.
            [Default: 29]

        rs (int): Ring step in pixels used during the resampling of images to 
            polar coordinates.
            [Default: 1]
        
        xr (int): x-range of translational search during alignment. 
            [Default: 1]
        
        yr (int): y-range of translational search during alignment. 
            [Default: 1]
        
        ts (float): Search step size (in pixels) of translational search. (Not
            entirely clear; used in refinement.)
            [Default 1.0]
        
        maxit (int): Number of iterations for reference-free alignment. 
            [Default: 30]
        
        CTF (bool): If set the data will be phase-flipped using CTF information 
            included in image headers.
            [Default: False][UNSUPPORTED]
        
        snr (float): Signal to noise ratio used if CTF parameter is True.
            [Default: 1.0][UNSUPPORTED]
        
        dst (float): Discrete angle used during group alignment.
            [Default: 90.0]
        
        FL (float): Frequency of the lowest stop band used in the tangent filter.
            [Default: 0.2]
        
        FH (float): Frequency of the highest stop band used in the tangent filter.
            [Default 0.45]
        
        FF (float): Fall-off value for the tangent filter. 
            [Default 0.2]
        
        init_iter (int): Maximum number of Generation iterations performed for
            a given subset. (Unclear what "subset" refers to.)
            [Default: 7]
        
        iter_reali (int): SAC stability check interval. Every iter_reali-th 
            iteration of SAC stability checking is performed.
            [Default: 1]

        stab_ali (int): Number of alignment iterations when checking stability.
            [Default: 5]
        
        thld_err (float): Threshold of pixel error when checking stability.
            Equals root mean square of distances between corresponding pixels
            from set of found transformations and theirs average transformation; 
            depends linearly on square of radius (parameter target_radius). 
            Units are in pixels.
            [Default: 0.7]

        img_per_grp (int): Number of images per class (maximum group size, also
            defines number of classes: K=(total number of images)/img_per_grp.
            [Default: 200]
        
        generation (int): Number of iterations in the current generation.
        
        random_seed (int): Set random seed manually for testing purposes.
            [Default: None]
        
        new (bool): Flag to use "new code"; set to False and not used.

    Returns:
        refi (UNKNOWN TYPE): Refinement as returned by isac_MPI_pap()

        all_ali_params (list): List containing 2D alignment parameters for all
            images; entries formatted as [angle, sx, sy, mirror].
    """

    #------------------------------------------------------[ mpi related part of the code]

    number_of_proc = Blockdata["nproc"]
    myid = Blockdata["myid"]
    main_node = Blockdata["main_node"]

    seed(myid)
    rand1 = randint(1,1000111222)
    seed(random_seed)
    rand2 = randint(1,1000111222)
    seed(rand1 + rand2)

    if generation == 0:
        ERROR("Generation should begin from 1, please reset it and restart the program", "iter_isac", 1, myid)
    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)
    ali_params_dir = "ali_params_generation_%d"%generation
    if os.path.exists(ali_params_dir):  
        ERROR('Output directory %s for alignment parameters exists, please either change its name or delete it and restart the program'%ali_params_dir, "iter_isac", 1, myid)
    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    if new: 
        alimethod = "SHC"
    else:  
        alimethod = ""

    color = 0                        # Blockdata["Ycolor"]
    key = Blockdata["myid"]          # Blockdata["Ykey"]
    group_comm = mpi.MPI_COMM_WORLD  # Blockdata["Ygroup_comm"]
    group_main_node = 0

    nx     = alldata[0].get_xsize()
    ndata  = len(alldata)
    data   = [None]*ndata
    tdummy = Transform({"type":"2D"})

    for im in range(ndata):
        # This is the absolute ID, the only time we use it is
        # when setting the members of 4-way output. All other times, the id in 'members' is 
        # the relative ID.
        alldata[im].set_attr_dict({"xform.align2d": tdummy, "ID": im})
        data[im] = alldata[im]

    avg_num = 0
    Iter = 1
    K = ndata/img_per_grp

    if myid == main_node:
        print("     We will process:  %d current images divided equally between %d groups"%(ndata, K))
        print("*************************************************************************************")

    #------------------------------------------------------[ generate random averages for each group ]

    if key == group_main_node:
        refi = generate_random_averages(data, K, 9023)
    else:
        refi = [spx.model_blank(nx, nx) for i in range(K)]

    for i in range(K):
        spx.bcast_EMData_to_all(refi[i], key, group_main_node, group_comm)

    # create d[K*ndata] matrix 
    orgsize = K*ndata

    if( Blockdata["myid_on_node"] == 0 ): size = orgsize
    else:  size = 0

    disp_unit = np.dtype("f4").itemsize

    win_sm, base_ptr  = mpi.mpi_win_allocate_shared( size*disp_unit , disp_unit, mpi.MPI_INFO_NULL, Blockdata["shared_comm"])
    size = orgsize
    if( Blockdata["myid_on_node"] != 0 ):
        base_ptr, = mpi.mpi_win_shared_query(win_sm, mpi.MPI_PROC_NULL)

    d = np.frombuffer(np.core.multiarray.int_asbuffer(base_ptr, size*disp_unit), dtype = 'f4')
    d = d.reshape(orgsize)
    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    #------------------------------------------------------[ Generate inital averages ] 

    refi = isac_MPI_pap(data, refi, d, maskfile=None, ir=ir, ou=ou, rs=rs, xrng=xr, yrng=yr, step=ts, 
            maxit=maxit, isac_iter=init_iter, CTF=CTF, snr=snr, rand_seed=-1, color=color, comm=group_comm, 
            stability=True, stab_ali=stab_ali, iter_reali=iter_reali, thld_err=thld_err,
            FL=FL, FH=FH, FF=FF, dst=dst, method = alimethod)

    mpi.mpi_win_free(win_sm)
    del d
    # print "  AFTER FIRST isac_MPI_pap"

    ## broadcast current_refim to all nodes
    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    if myid == main_node:
        all_ali_params = [None]*len(data)
        for i,im in enumerate(data):
            alpha, sx, sy, mirror, scale = spx.get_params2D(im)
            all_ali_params[i] = [alpha, sx, sy, mirror]

        print("****************************************************************************************************")
        print("*         Generation finished                 "+time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime())+"                            *")
        print("****************************************************************************************************")
        return refi, all_ali_params

    else:  
        return [], []

def isac_MPI_pap( 
    stack, refim, d,
    maskfile=None,
    ir=1, ou=-1, rs=1,
    xrng=0, yrng=0, step=1,
    maxit=30, isac_iter=10,
    CTF=False, snr=1.0, rand_seed=-1,
    color=0, comm=-1,
    stability=False, stab_ali=5, iter_reali=1,
    thld_err=1.732,
    FL=0.1, FH=0.3, FF=0.2, dst=90.0,
    method=""):

    """
    ISAC core function.

    Args:
        stack (UNKNOWN TYPE): list of images (filename also accepted)

        refim (list OR filename): Class averages. (Providing a filename exits.)

        d (numy.ndarray): Array holding pairwise distances between images.

        maskfile (image OR filename): Image containing mask (filename also 
            accepted).  

        ir (int): Inner ring value (in pixels) of the resampling to polar 
            coordinates.
            [Default: 1]

        ou (int): Target particle radius used when ISAC processes the data.
            Images will be scaled to conform to this value.
            [Default: 29]

        rs (int): Ring step in pixels used during the resampling of images to
            polar coordinates.
            [Default: 1]

        xrng (int): x-range of translational search during alignment. 
            [Default: 0]
        
        yrng (int): y-range of translational search during alignment. 
            [Default: 0]

        step (float): Search step size (in pixels) of translational search. 
            (Not entirely clear; used in refinement.)
            [Default 1]

        maxit (int): Number of iterations for reference-free alignment. 
            [Default: 30]

        isac_iter (UNKNOWN TYPE): Maximum number of Generation iterations 
            performed for a given subset. (Unclear what "subset" refers to.)
            [Default: 10]

        CTF (bool): If set the data will be phase-flipped using CTF information
            included in image headers.
            [Default: False][UNSUPPORTED]

        snr (float): Signal to noise ratio used if CTF parameter is True.
            [Default: 1.0][UNSUPPORTED]

        rand_seed (int): Set random seed manually for testing purposes.
            [Default: -1]

        color (mpi color): set to 0; unclear if this is still relevant.
            [Defailt: 0]

        comm (mpi communicator): set to MPI_COMM_WORLD (globally available; redundant parameter)
            [Default: -1]

        stability (bool): If True, ISAC performs stability testing.
            [Default: True] 

        stab_ali (bool): Used only when stability testing is performed.
            [Default: 5]

        iter_reali (UNKNOWN TYPE): Used only when stability=True. For each
            iteration i: if (i % iter_reali == 0) stability check is performed.
            [Default: 1]

        thld_err (float): Threshold of pixel error when checking stability.
            Equals root mean square of distances between corresponding pixels
            from set of found transformations and theirs average transformation; 
            depends linearly on square of radius (parameter target_radius). 
            Units are in pixels.
            [Default: 1.732]

        FL (float): Frequency of the lowest stop band used in the tangent filter.
            [Default: 0.1]
        
        FH (float): Frequency of the highest stop band used in the tangent filter.
            [Default 0.3]
        
        FF (float): Fall-off value for the tangent filter. 
            [Default 0.2]

        dst (float): Discrete angle used during group alignment.
            [Default: 90.0]

        method (string): Stock method (SHC) for alignment.
            [Default: ""]

    Returns:
        alldata (list): Class averages (format unclear).
    """

    #------------------------------------------------------[ initialize mpi ]

    if comm == -1: comm = mpi.MPI_COMM_WORLD    

    number_of_proc = mpi.mpi_comm_size(comm)
    myid = mpi.mpi_comm_rank(comm)
    my_abs_id = mpi.mpi_comm_rank(mpi.MPI_COMM_WORLD)
    main_node = 0

    #------------------------------------------------------[ initialize generation ]

    first_ring = int(ir)
    last_ring  = int(ou)
    rstep      = int(rs)
    max_iter   = int(isac_iter)

    # TODO: either remove it or put a helpful error message here -_-
    if type(stack) == type(""):
        print("  SHOULD NOT BE HERE")
        sys.exit()
    else:
        alldata = stack

    # image dimensions
    nx = alldata[0].get_xsize()
    ny = alldata[0].get_ysize()

    # No. of images
    nima = len(alldata)

    # reset all alignment parameters
    for im in range(nima):
        spx.set_params2D(alldata[im], [0.0, 0.0 ,0.0, 0, 1.0])

    # indices determining the workload of this MPI process
    image_start, image_end = MPI_start_end(nima, number_of_proc, myid)

    # set mask (circular mask by default)
    if maskfile:
        if type(maskfile) is bytes:
            mask = get_image(maskfile)
        else: 
            mask = maskfile
    else : 
        mask = spx.model_circle(last_ring, nx, nx)

    # read references from file (if given a filename)
    if type(refim) == type(""):
        refi = EMData.read_images(refim)

    # each process creates a working copy of the references
    else:
        refi = [None for i in range(len(refim))]
        for i in range(len(refim)):
            refi[i] = refim[i].copy()

        # NOTE: It's safer to make a hard copy here. Although I am not sure, I
        # believe a shallow copy has messed up the program. This is really 
        # strange. It takes much memory without any need. [PAP 01/17/2015]
        # However, later I made changes so refi is deleted from time to time.

        # NOTE: Shallow copies will result in each MPI process referencing the
        # same reference stack, which probably is the source of the troubles
        # mentioned above. [FS 07/09/2019]

    # No. of references / classes
    numref = len(refi)

    # images are assumed to be square; center coordinates follow SPIDER convention
    cnx = nx/2+1
    cny = cnx

    # interpolation mode: "F"ull or "H"alf circle interpolation
    mode = "F" # this will almost always be "F" unless there is a very specific reason to use "H"

    #--------------------------------------------------[ main iteration setup ]

    # calculate number and weighting of rings given inner/outer radius and step size
    numr = Numrinit(first_ring, last_ring, rstep, mode)
    wr = ringwe(numr, mode)

    # initialize random sequence
    if rand_seed > -1:
        seed(rand_seed)
    else:
        seed(randint(1,2000111222))
    
    # attempt to making sure the random sequences per thread do not overlap
    if myid != main_node:   
        jumpahead(17*myid + 12345)

    # initialize the member set for each reference (list of image indices
    # denoting which images were averaged to form a reference)
    previous_agreement = 0.0
    previous_members = [None]*numref
    for j in range(numref):
        previous_members[j] = set()

    # a new variable!
    fl = FL

    # initialize main iteration counters and abort flag
    Iter = -1
    main_iter = 0
    terminate = 0

    #------------------------------------------------------[ main iteration loop ]

    while( (main_iter < max_iter) and (terminate == 0) ):
        
        Iter += 1
        if my_abs_id == main_node: 
            print("Iteration within isac_MPI Iter =>", Iter, "   main_iter = ", main_iter, " len data = ", image_end-image_start,"   ",time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()))
        mashi = cnx-ou-2




        #======================================================================[ STEP 01: Compute new averages ]

        if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Preparing references.." ); sys.stdout.flush()

        # prepare the references (apply polar conversion, FFT, and ring weights)
        for j in range(numref):
            refi[j].process_inplace("normalize.mask", {"mask":mask, "no_sigma":1})  # normalize reference images to N(0,1)
            cimage = Util.Polar2Dm(refi[j] , cnx, cny, numr, mode)  # converting reference images to polar cordinates 
            Util.Frngs(cimage, numr)        # Applying FFT on the reference images 
            Util.Applyws(cimage, numr, wr)  # apply weights to FTs of rings
            refi[j] = cimage.copy()         # refi now holds the converted averages

        # peak_list stores alignment parameters for each reference and image
        peak_list = [ np.zeros(4*(image_end-image_start), dtype=np.float32) for i in range(numref) ]
        
        # d stores the actual correlation value for each reference and image
        if( Blockdata["myid_on_node"] == 0 ): 
            d.fill(0.0) # NOTE: this array is defined in the parent function

        # NOTE: peak_list and d will later be used to determine which reference
        # each image matches with the most and what the alignment parameters
        # need to be applied to the image in order to achieve that match.

        #--------------------------------------------------[ individualy align each image with every reference ]

        if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Multireference alignment.." ); sys.stdout.flush()

        for im in range(image_start, image_end):

            # check whether existing shifts are in permissible range (if not, reset)
            alpha,  sx,  sy,  _, _ = spx.get_params2D( alldata[im] )
            alphai, sxi, syi, _    = spx.inverse_transform2( alpha, sx, sy )

            # reset shifts in case they are out of range
            if( abs(sxi)>cnx-ou-2 or abs(syi)>cnx-ou-2 ):
                sxi = 0.0
                syi = 0.0
                spx.set_params2D( alldata[im], [0.0,0.0,0.0,0,1.0] )

            # normalize images
            alldata[im].process_inplace( "normalize.mask", {"mask":mask, "no_sigma":0} )  # subtract average under the mask
            
            # find permissible translational search ranges (from, to)
            txrng = search_range(nx, ou, sxi, xrng, "ISAC2")
            txrng = [txrng[1],txrng[0]]
            tyrng = search_range(ny, ou, syi, yrng, "ISAC2")
            tyrng = [tyrng[1],tyrng[0]]

            #----------------------------------------------[ singular multireference alignment ]

            # compute alignment parameters for SINGLE image <alldata[im]> and ALL references <refi>
            # NOTE: multiref_polar_ali_2d_peaklist() includes conversion of image data into polar coordinates AND an FFT
            temp = Util.multiref_polar_ali_2d_peaklist(alldata[im], refi, txrng, tyrng, step, mode, numr, cnx+sxi, cny+syi)
            
            # temp[i*5 : (i+1)*5] = [correlation, angle, shift_x, shift_y, mirror] for aligning image <alldata[im]> & refi[i]
            for iref in range(numref):
                # combine existing shifts with newly computed alignment parameters
                [alphan, sxn, syn, mn] = spx.combine_params2( 0.0, -sxi, -syi, 0, 
                                                              temp[iref*5+1], temp[iref*5+2], 
                                                              temp[iref*5+3], int(temp[iref*5+4]) )
                # store the alignment parameters of this individual image and all references
                peak_list[iref][(im-image_start)*4+0] = alphan
                peak_list[iref][(im-image_start)*4+1] = sxn
                peak_list[iref][(im-image_start)*4+2] = syn
                peak_list[iref][(im-image_start)*4+3] = mn
                # store the correlation value of this individual image and all references 
                d[iref*nima+im] = temp[iref*5]

            del temp

        del refi  # delete the references; will be re-assigned and filled w/ the updated references below

        #--------------------------------------------------[ collect alignment results ]

        if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Collecting alignment results.." ); sys.stdout.flush()

        # - Each process has a full-sized d-matrix but only fills part of it.
        # - Now we need to collect all values in the d-matrix on the main_node.
        # - To do so we simply sum up the d matrices across all processes.
        # - If there is only one node (no_of_groups == 1) then this isn't necessary.
        # - See here how MPI_Reduce behaves when each process holds an array:
        #   -> https://mpitutorial.com/tutorials/mpi-reduce-and-allreduce/#mpi_reduce
        #   -> https://i.imgur.com/WWp4OhK.png  // NOTE: NOT a sum over the individual arrays!

        if( Blockdata["subgroup_myid"] > -1 ):
            if(Blockdata["no_of_groups"] > 1):
                
                for j in range(numref):
                    dbuf = np.zeros(nima, dtype=np.float32)
                    np.copyto(dbuf,d[j*nima:(j+1)*nima])
                    
                    dbuf = mpi.mpi_reduce(dbuf, nima, MPI_FLOAT, MPI_SUM, main_node, Blockdata["subgroup_comm"])
                    if( Blockdata["subgroup_myid"] == 0 ):  
                        np.copyto(d[j*nima:(j+1)*nima],dbuf)

                del dbuf
        
        mpi.mpi_barrier(comm)

        #--------------------------------------------------[ determine alignment match ]
        
        if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Determining alignment match.." ); sys.stdout.flush()

        # At this point only the main node has all the info, so it is in this
        # process where we determine the best reference match for each image.

        # At the heart of this process is some grisly C function with the usual
        # level of non-existing documentation:
        #    https://blake.grid.bcm.edu/eman2/doxygen_html/util__sparx_8cpp_source.html#l18381

        # find maximum in the peak list to determine highest subject/reference match
        if myid == main_node:

            id_list_long = Util.assign_groups(str(d.__array_interface__['data'][0]), numref, nima) # string with memory address is passed as parameters

            id_list = [[] for i in range(numref)]
            maxasi = nima/numref  # maxasi = max. assignment

            for i in range(maxasi*numref): id_list[i/maxasi].append(id_list_long[i])
            for i in range(nima%maxasi):   id_list[id_list_long[-1]].append(id_list_long[maxasi*numref+i])
            for iref in range(numref):     id_list[iref].sort()

            del id_list_long

            belongsto = [0]*nima
            for iref in range(numref):
                for im in id_list[iref]:
                    belongsto[im] = iref # image <im> has highest match with reference <iref>

            del id_list

        else:
            belongsto = [0]*nima

        # broadcast assignment result to all mpi nodes
        mpi.mpi_barrier(comm)
        belongsto = mpi.mpi_bcast(belongsto, nima, mpi.MPI_INT, main_node, comm)
        belongsto = list(map(int, belongsto))

        #--------------------------------------------------[ update the averages / create new references ]

        if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Updating references.." ); sys.stdout.flush()

        # Now we know which images match with the same average/reference. Each
        # reference is now updated by averaging of the images assigned to it.

        members =   [0]*numref   # stores number of images assigned to each reference
        sx_sum  = [0.0]*numref   # accumulated shift in x-direction across all images forming each reference
        sy_sum  = [0.0]*numref   # accumulated shift in y-direction across all images forming each reference

        # start new set of reference images (refi)
        refi = [ spx.model_blank(nx,ny) for j in range(numref) ]

        # everyone aligns their images and adds them to their matching average
        for im in range(image_start, image_end):
            
            # get alignment parameters
            matchref = belongsto[im]
            alphan = float(peak_list[matchref][(im-image_start)*4+0])
            sxn    = float(peak_list[matchref][(im-image_start)*4+1])
            syn    = float(peak_list[matchref][(im-image_start)*4+2])
            mn     =   int(peak_list[matchref][(im-image_start)*4+3])

            # sum up the total shift applied to each image forming a new reference
            if mn == 0: sx_sum[matchref] += sxn
            else:       sx_sum[matchref] -= sxn
            sy_sum[matchref] += syn

            # apply alignment parameters to image and add to the relevant average
            # NOTE: the req. division to compute the actual average happens below
            Util.add_img( refi[matchref], rot_shift2D(alldata[im], alphan, sxn, syn, mn) )
            members[matchref] += 1

        # add up the total shift sums across all processes (collect results on the main_node)
        sx_sum  = mpi.mpi_reduce( sx_sum,  numref, mpi.MPI_FLOAT, mpi.MPI_SUM, main_node, comm )
        sy_sum  = mpi.mpi_reduce( sy_sum,  numref, mpi.MPI_FLOAT, mpi.MPI_SUM, main_node, comm )
        members = mpi.mpi_reduce( members, numref, mpi.MPI_INT,   mpi.MPI_SUM, main_node, comm )

        if myid != main_node:
            sx_sum  = [0.0]*numref
            sy_sum  = [0.0]*numref
            members = [0.0]*numref

        # main_node broadcasts final shift sums and member count
        sx_sum  = mpi.mpi_bcast( sx_sum,  numref, mpi.MPI_FLOAT, main_node, comm )
        sy_sum  = mpi.mpi_bcast( sy_sum,  numref, mpi.MPI_FLOAT, main_node, comm )
        members = mpi.mpi_bcast( members, numref, mpi.MPI_INT,   main_node, comm )
        sx_sum  = list( map(float, sx_sum) )
        sy_sum  = list( map(float, sy_sum) )
        members = list( map(int, members) )

        # compute the average shift applied to each image forming reference[j]
        for j in range(numref):
            sx_sum[j] /= float(members[j])
            sy_sum[j] /= float(members[j])

        # shift every image by the above computed average shift; this means
        # their average now sits at the center of the image
        for im in range(image_start, image_end):
            matchref = belongsto[im]
            alphan = float(peak_list[matchref][(im-image_start)*4+0])
            sxn    = float(peak_list[matchref][(im-image_start)*4+1])
            syn    = float(peak_list[matchref][(im-image_start)*4+2])
            mn     =   int(peak_list[matchref][(im-image_start)*4+3])

            if mn == 0: spx.set_params2D(alldata[im], [alphan, sxn-sx_sum[matchref], syn-sy_sum[matchref], mn, 1.0])
            else:       spx.set_params2D(alldata[im], [alphan, sxn+sx_sum[matchref], syn-sy_sum[matchref], mn, 1.0])

        del peak_list

        # NOTE: The averages have already been computed above, so right now the
        # averages of the images w/ updated alignment parameters are NOT the
        # same as the averages we have computed above! We rectify this now:

        for j in range(numref):
            spx.reduce_EMData_to_root(refi[j], myid, main_node, comm)

            if myid == main_node:
                Util.mul_scalar(refi[j], 1.0/float(members[j]))     # finally divide by no. of group members to get the actual average
                refi[j] = filt_tanl(refi[j], fl, FF)                # apply the usual tangens filter to the new reference
                refi[j] = fshift(refi[j], -sx_sum[j], -sy_sum[j])   # center the new reference by adding the average shift computed above
                spx.set_params2D(refi[j], [0.0, 0.0, 0.0, 0, 1.0])

        #--------------------------------------------------[ experimental centering ]

        if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Experimental centering.." ); sys.stdout.flush()

        # this is most likely meant to center them, if so, it works poorly,
        # it has to be checked and probably a better method used [PAP 01/17/2015]

        # The step below aligns each reference with our default circular mask
        # and then moves the reference using its new alignment parameters. As
        # you can see above, Pavel is not a fan. It's still done though, so who
        # knows. [FS 07/09/2019]

        # step 01: align references with mask
        if myid == main_node:
            dummy = within_group_refinement(refi, mask, True, first_ring, last_ring, rstep, [xrng], [yrng], [step], dst, maxit, FH, FF)
            ref_ali_params = []

            for j in range(numref):
                alpha, sx, sy, mirror, scale = spx.get_params2D(refi[j])
                refi[j] = rot_shift2D(refi[j], alpha, sx, sy, mirror)
                ref_ali_params.extend([alpha, sx, sy, mirror])
        else:
            ref_ali_params = [0.0]*(numref*4)

        # step 02: broadcast centered references and their new alignment parameters
        ref_ali_params = mpi.mpi_bcast( ref_ali_params, numref*4, mpi.MPI_FLOAT, main_node, comm )
        ref_ali_params = list( map(float, ref_ali_params) )

        for j in range(numref):
            spx.bcast_EMData_to_all( refi[j], myid, main_node, comm )

        # step 03: add the new parameters to the existing parameters of the
        # images that make up the individual averages
        for im in range(image_start, image_end):
            matchref = belongsto[im]
            alpha, sx, sy, mirror, scale = spx.get_params2D(alldata[im])
            alphan, sxn, syn, mirrorn = spx.combine_params2(alpha, sx, sy, mirror,
                                                            ref_ali_params[matchref*4+0], 
                                                            ref_ali_params[matchref*4+1],
                                                            ref_ali_params[matchref*4+2], 
                                                            int(ref_ali_params[matchref*4+3]))
            spx.set_params2D( alldata[im], [alphan, sxn, syn, int(mirrorn), 1.0] )

        mpi.mpi_barrier( mpi.MPI_COMM_WORLD )




        #======================================================================[ STEP 02: Stability check ]

        # decision of whether to do the stability test (by default ISAC uses FL=0.2 and FH=0.45)
        # NOTE: it's not clear why the decision for in group alignment depends on the filter value
        fl += 0.05
        if fl >= FH:
            fl = FL
            do_within_group = 1
        else:
            do_within_group = 0

        # Here stability does not need to be checked for each main iteration,
        # it only needs to be done for every 'iter_reali' iterations. If one
        # really wants it to be checked each time simple set iter_reali to 1,
        # which is the default value right now. [PAP, no date]
        check_stability = ( stability and (main_iter%iter_reali==0) )

        #--------------------------------------------------[ broadcast alignment parameters ]

        if do_within_group == 1 or Blockdata["hop_like_a_bunny"]==True:

            if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Broadcasting alignment parameters.." ); sys.stdout.flush()

            for i in range(number_of_proc):
                im_start, im_end = MPI_start_end( nima, number_of_proc, i )

                # one by one each process broadcasts their batch of computed alignment parameters
                if myid == i:
                    ali_params = []
                    for im in range(image_start, image_end):
                        alpha, sx, sy, mirror, _ = spx.get_params2D( alldata[im] )
                        ali_params.extend( [alpha, sx, sy, mirror] )
                else:
                    ali_params = [0.0]*((im_end-im_start)*4)
                
                ali_params = mpi.mpi_bcast( ali_params, len(ali_params), mpi.MPI_FLOAT, i, comm )
                ali_params = list(map(float, ali_params))
                
                # everyone writes the newly broadcast values directly into the EMData image objects
                for im in range(im_start, im_end):
                    alpha  =     ali_params[(im-im_start)*4+0]
                    sx     =     ali_params[(im-im_start)*4+1]
                    sy     =     ali_params[(im-im_start)*4+2]
                    mirror = int(ali_params[(im-im_start)*4+3])
                    spx.set_params2D( alldata[im], [alpha, sx, sy, mirror, 1.0] )

            main_iter += 1 # not sure why we are not doing this earlier
            
            gpixer = [] # gpixer = [g]athered [pi]xel [er]ror

            mpi.mpi_barrier( mpi.MPI_COMM_WORLD )




            #==============================================================================================================[ GPU ]

            aln_param = { k: [[] for _ in range(stab_ali)] for k in range(numref) }
            # aln_params[k] holds aln params for all images assigned to class/reference[k]:
            #     [[ (a,x,y,m)..(a,x,y,m)..(a,x,y,m) ],    <-- stability iteration 0
            #      [ (a,x,y,m)..(a,x,y,m)..(a,x,y,m) ],    <-- stability iteration 1
            #                      ..                                         ..
            #      [ (a,x,y,m)..(a,x,y,m)..(a,x,y,m) ]]    <-- stability iteration stab_ali-1
            #                       \__________________________alignment parameters for img_i

            mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

            def gpu_refinement( cuda_device_id, cid_load ):

                # Step 01: For N available gpus MPI processes 0 to N-1 will perform
                # the multigroup refinement and fill 1/N-th of the aln_params table
                if myid == cuda_device_id: 

                    # get local views on the data
                    sbj_obj_list, sbj_cid_list, sbj_idx_list = [], [], []
                    for i, cid in enumerate(cid_load):
                        idx_obj_list  = [ (img_idx,alldata[img_idx]) for img_idx in range(nima) if belongsto[img_idx] == cid ]
                        sbj_idx_list += [ idx_obj_tuple[0] for idx_obj_tuple in idx_obj_list ]
                        sbj_obj_list += [ idx_obj_tuple[1] for idx_obj_tuple in idx_obj_list ]
                        sbj_cid_list += [i]*len(idx_obj_list) # NOTE: cid of sbj_obj_list[idx] is cid_load[sbj_cid_list[idx]]

                    cid_idx_list = np.append( np.where(np.roll(sbj_cid_list,1) != sbj_cid_list)[0], len(sbj_cid_list) )
                    if len(cid_idx_list) == 1: cid_idx_list = [0, cid_idx_list[0]] # only needed if there is a single class

                    # run the group refinement and fill the (local) aln_params table
                    for stability_iteration in range(stab_ali):

                        applications.multigroup_refinement_gpu(
                            sbj_obj_list,
                            sbj_cid_list,
                            ou, xrng, yrng,
                            maxit, FH, FF, True,
                            cuda_device_id=myid )

                        for i,cid in enumerate(cid_load):
                            for sbj_obj in sbj_obj_list[ cid_idx_list[i] : cid_idx_list[i+1] ]:
                                angle, shift_x, shift_y, mirror, _ = spx.get_params2D( sbj_obj )
                                aln_param[cid][stability_iteration].extend( [angle, shift_x, shift_y, mirror] )

            def gpu_refinement_send_recv( cuda_device_id, cid_load ):

                # Step 02: Once all data is processed we distribute the results
                # across all available MPI processes. NOTE: Every process only
                # receives those aln_params entries that it actually deals with.
                # NO PROCESS actually has the full aln_params table filled with
                # the alignment data of ALL classes.
                for i, cid in enumerate(cid_load):

                    # no need to send data if the target process is already processing the data
                    if cid%number_of_proc == cuda_device_id: continue

                    # send data size and data itself
                    if myid == cuda_device_id:
                        aln_param_transfer_len = len(aln_param[cid]) * len(aln_param[cid][0]) # NOTE: aln_param[k] is a list of lists
                        mpi.mpi_send( aln_param_transfer_len, 1, mpi.MPI_INT, cid%number_of_proc, cid*10+0, mpi.MPI_COMM_WORLD )
                        mpi.mpi_send( aln_param[cid], aln_param_transfer_len, mpi.MPI_FLOAT, cid%number_of_proc, cid*10+1, mpi.MPI_COMM_WORLD )

                    # receive data size and data itself
                    elif cid%number_of_proc == myid:
                        # receive size of data
                        aln_param_transfer_len = mpi.mpi_recv( 1, mpi.MPI_INT, cuda_device_id, cid*10+0, mpi.MPI_COMM_WORLD )
                        aln_param_transfer_len = int( aln_param_transfer_len[0] ) # mpi_recv() returns a numpy array
                        # receive the actual data
                        aln_param[cid] = mpi.mpi_recv( aln_param_transfer_len, mpi.MPI_FLOAT, cuda_device_id, cid*10+1, mpi.MPI_COMM_WORLD )
                        aln_param[cid] = aln_param[cid].reshape( stab_ali, len(aln_param[cid])/stab_ali ) # mpi_recv() returns flattened array
                        aln_param[cid] = aln_param[cid].tolist() # convert numpy array to original data format (list of lists)

            def gpu_refinement_broadcast( cuda_device_id, cid_load ):

                sbj_idx_list = []
                if myid == cuda_device_id:
                    for cid in cid_load:
                        sbj_idx_list += [ img_idx for img_idx in range(nima) if belongsto[img_idx] == cid ]

                # Step 03: Broadcast the alignment parameters set when calling the
                # group refinement.
                sbj_idx_list_len = -1 if myid!=cuda_device_id else len(sbj_idx_list)
                sbj_idx_list_len = mpi.mpi_bcast( sbj_idx_list_len, 1, mpi.MPI_INT, cuda_device_id, comm )
                sbj_idx_list_len = int( sbj_idx_list_len[0] )

                sbj_idx_list = [0.0]*sbj_idx_list_len if myid!=cuda_device_id else sbj_idx_list
                sbj_idx_list = mpi.mpi_bcast( sbj_idx_list, len(sbj_idx_list), mpi.MPI_INT, cuda_device_id, comm )
                sbj_idx_list = list( map(int, sbj_idx_list) )

                for sbj_idx in sbj_idx_list:
                    aln_param_tuple = spx.get_params2D( alldata[sbj_idx] )[0:4] if myid==cuda_device_id else [0.0]*4
                    aln_param_tuple = mpi.mpi_bcast( aln_param_tuple, 4, mpi.MPI_FLOAT, cuda_device_id, comm )
                    aln_param_tuple = list( map(float, aln_param_tuple) )
                    aln_param_tuple = aln_param_tuple[0:3] + [int(aln_param_tuple[3])] + [1.0]
                    spx.set_params2D( alldata[sbj_idx], aln_param_tuple )

            #==================================================[ run class refinement on GPUs and distribute results ]

            if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Running GPU class refinement.." ); sys.stdout.flush()

            global GPU_DEVICES, GPU_CLASS_LIMIT

            cid_load = {}
            for cuda_device_id in GPU_DEVICES:
                cid_load[cuda_device_id] = [ k for k in range(numref) if k%len(GPU_DEVICES)==cuda_device_id ]

            if myid in cid_load:
                for cuda_device_id in GPU_DEVICES:
                    for batch_start in range(0, len(cid_load[cuda_device_id]), GPU_CLASS_LIMIT):
                        gpu_refinement( cuda_device_id, cid_load[cuda_device_id][batch_start:batch_start+GPU_CLASS_LIMIT] )
                        # - myid==cuda_device_id thread runs refinement
                        # - every other thread skips
                        # => refinement done in parallel on all GPU nodes
            mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

            for cuda_device_id in GPU_DEVICES:
                gpu_refinement_send_recv( cuda_device_id, cid_load[cuda_device_id] )
                # - myid==cuda_device_id thread sends its aln_param[] table entries
                # - target threads receives aln_param[] table entries
                # - every other thread skips
                # => aln_param[] table entries are distributed sequentially (only have blockind send/recv available)
                # => because of sequential order, only one sender at any given time
            mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

            for cuda_device_id in GPU_DEVICES:
                gpu_refinement_broadcast( cuda_device_id, cid_load[cuda_device_id] )
                # - myid==cuda_device_id thread broadcasts updated particle headers
                # - every other thread listens
                # => particle header update is broadcast sequentially
                # => because of sequential order, only one broadcast at any given time
            mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

            #==================================================[ run the multi-alignment stability testing on CPUs ]

            if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Running stability testing.." ); sys.stdout.flush()

            for k in range( myid, numref, number_of_proc ):
                
                idx_obj_list = [ (i,alldata[i]) for i in range(nima) if belongsto[i] == k ]
                sbj_idx_list = [ idx_obj_tuple[0] for idx_obj_tuple in idx_obj_list ]
                sbj_obj_list = [ idx_obj_tuple[1] for idx_obj_tuple in idx_obj_list ]

                stable_set, mirror_consistent_rate, err = multi_align_stability( aln_param[k], 0.0, 10000.0, thld_err, False, last_ring*2 )
                gpixer.append( err )

                # artificially expand the stable_set in case it is too small
                # NOTE: stable_set[i] = [pixel_error, img_idx, (avg_aln_params)]
                while len(stable_set) < 5:
                    assert len(sbj_idx_list) >= 5, "Gefahr! Infinite loop bullshittery >_<"
                    duplicate = True
                    while duplicate:
                        duplicate  = False
                        rnd_select = randint( 0, len(sbj_idx_list)-1 ) # if there's no imgs left to select this will loop endlessly
                        for stable_img in stable_set:
                            if rnd_select == stable_img[1]:
                                duplicate = True
                    stable_set.append( [100.0, rnd_select, [0.0,0.0,0.0,0]] )
                    # NOTE: this doesn't really seem to be an ideal solution

                # overwrite references with the average of the stable_set
                # NOTE: stable_set[i] = [pixel_error, img_idx, (avg_aln_params)]
                stable_obj_list, stable_idx_list = [], []
                for stable_img in stable_set:
                    img_idx = stable_img[1]
                    stable_idx_list.append( sbj_idx_list[img_idx] )
                    stable_obj_list.append( sbj_obj_list[img_idx] )
                    spx.set_params2D( stable_obj_list[-1], [stable_img[2][0], stable_img[2][1], stable_img[2][2], int(stable_img[2][3]), 1.0] )

                stable_idx_list.sort() # shouldn't be necessary but sure

                # update references with class membership information
                refi[k] = filt_tanl( ave_series(stable_obj_list), FH, FF )
                refi[k].set_attr( "n_objects", len(stable_idx_list) )
                refi[k].set_attr( "members", stable_idx_list )

            mpi.mpi_barrier(comm)

            #======================================================================[ STEP 03: Broadcast alignment params and references ]

            if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Broadcasting new alignment parameters and references.." ); sys.stdout.flush()

            # broadcast alignment parameter update (header information only)
            for img_idx in range(nima):
                # source process
                src_proc = belongsto[img_idx]%number_of_proc
                # broadcast
                aln_param_tuple = spx.get_params2D(alldata[img_idx])[0:4] if myid==src_proc else [0.0]*4
                aln_param_tuple = mpi.mpi_bcast( aln_param_tuple, 4, mpi.MPI_FLOAT, src_proc, comm )
                aln_param_tuple = list( map(float, aln_param_tuple) )
                # update
                aln_param_tuple = aln_param_tuple[0:3] + [int(aln_param_tuple[3])] + [1.0]
                spx.set_params2D( alldata[img_idx], aln_param_tuple )

            # broadcast
            for k in range(numref):
                # NOTE: bcast_EMData_to_all() ONLY transfers the image itself
                # but NO header information! Because of this the above computed
                # membership (header) information is ONLY available to those 
                # processes that computed it. Only below is this information
                # broadcast and collected (on the main_node).
                spx.bcast_EMData_to_all( refi[k], myid, k%number_of_proc, comm )

            mpi.mpi_barrier(comm)




            #======================================================================[ STEP 04: Check class membership convergence ]

            if myid==0: print( "["+str(myid)+"]", time.strftime("%Y-%m-%d %H:%M:%S ::", time.localtime()), "Checking class membership convergence.." ); sys.stdout.flush()

            # As stated above, at this point the membership information for
            # each reference is only available to certain processes. Here
            # we collect it all on the main_node to then determine the 
            # stability of the membership sets across iterations.
            for k in range(numref):
                src_proc = k%number_of_proc
                if src_proc != main_node:

                    # source proccesses: send data size and data itself
                    if myid == src_proc:
                        members = refi[k].get_attr("members")
                        mpi.mpi_send( len(members), 1, mpi.MPI_INT, main_node, 1111, mpi.MPI_COMM_WORLD )
                        mpi.mpi_send( members, len(members), mpi.MPI_INT, main_node, 2222, mpi.MPI_COMM_WORLD )

                    # main process: receive data size and data itself
                    if myid == main_node:
                        members_transfer_len = int( mpi.mpi_recv(1, mpi.MPI_INT, src_proc, 1111, mpi.MPI_COMM_WORLD)[0] )
                        members = mpi.mpi_recv( members_transfer_len, mpi.MPI_INT, src_proc, 2222, mpi.MPI_COMM_WORLD )
                        members = list( map(int, members) )
                        refi[k].set_attr_dict( {"members": members, "n_objects": members_transfer_len} )

            # Compare membership information of the previous iteration with the new
            # membership information of this iteration. If the set of members is 
            # stable we are done here.
            if myid == main_node:

                # determine overlap of previous and current class membership sets
                totprevious = 0.0
                totcurrent  = 0.0
                common      = 0.0

                for k in range(numref):
                    totprevious += len( previous_members[k] )
                    members      = set( refi[k].get_attr('members') )
                    totcurrent  += len( members )
                    common      += len( previous_members[k].intersection(members) )
                    previous_members[k] = members

                # determine sufficent convergence in membership
                agreement = common / float(totprevious + totcurrent - common)
                delta     = agreement - previous_agreement
                
                if( (agreement>0.5) and (delta > 0.0) and (delta < 0.05) ): 
                    terminate = 1
                else:
                    terminate = 0
                
                # sound off on the current state of convergence
                previous_agreement = agreement
                print( ">>>  Assignment agreement with previous iteration  %5.1f" % (agreement*100), "   ", time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()) )
            
            terminate = spx.bcast_number_to_all( terminate, source_node=main_node )

            # final check: are we done?
            if( check_stability and ( (main_iter == max_iter) or (terminate == 1) ) ):
                
                # gather the logged pixel error values and show a histogram
                gpixer = spx.wrap_mpi_gatherv(gpixer, main_node, comm)

                if my_abs_id == main_node and color == 0:
                    lhist = 12
                    region, histo = hist_list(gpixer, lhist)
                    print( "\n=== Histogram of average within-class pixel errors prior to class pruning ===" )
                    for lhx in range(lhist):
                        print( "     %10.3f     %7d" % (region[lhx], histo[lhx]) )
                    print( "=============================================================================\n" )

                del gpixer # don't need this anymore

        mpi.mpi_barrier(comm) # whithin group stability test only ends here




    #==========================================================================[ STEP 05: Report final group stats ]

    # main_node prints a histogram of the final group sizes of this main iteration
    if myid == main_node:
        
        i = [ refi[j].get_attr("n_objects") for j in range(numref) ]
        lhist = max( 12, numref/2 )
        region, histo = hist_list(i, lhist)

        print( "\n=== Histogram of group sizes ================================================" )
        for lhx in range(lhist):  
            print( "     %10.1f     %7d" % (region[lhx], histo[lhx]) )
        print( "=============================================================================\n" )

    mpi.mpi_barrier(comm)

    return refi

def do_generation(main_iter, generation_iter, target_nx, target_xr, target_yr, target_radius, options):
    """
    Perform one iteration of ISAC processing within current generation.

    Args:
        main_iter (int): Number of SAC main iterations, i.e., the number of 
            runs of cluster alignment for stability evaluation in SAC.
            [Default: 3]

        generation_iter (int): Number of iterations in the current generation.

        target_nx (int): Target particle image size. This is the actual image
            size on which ISAC will process data. Images will be scaled 
            according to target particle radius and pruned/padded to achieve
            target_nx size. If xr > 0 (see below), the final image size for 
            ISAC processing equals target_nx + xr -1.
            [Default: 76]

        target_xr (int): x-range of translational search during alignment. 
            [Default: 1]

        target_yr (int): y-range of translational search during alignment. 
            [Default: 1]

        target_radius (int): Target particle radius used when ISAC processes
            the data. Images will be scaled to conform to this value.
            [Default: 29]

        options (options object): Provided by the Python OptionParser. This
            structure contains all command line options (option "--value" is
            accessed by options.value).

    Returns:
        keepdoing_main (bool): Indicates the main ISAC iteration should stop.

        keepdoing_generation (bool): Indicates the iterations within this 
            generation should stop.
    """

    global Blockdata

    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    if( Blockdata["myid"] == Blockdata["main_node"] ):
        plist = spx.read_text_file(
            os.path.join(Blockdata["masterdir"],
            "main%03d"%main_iter,
            "generation%03d"%(generation_iter-1),
            "to_process_next_%03d_%03d.txt"%(main_iter,generation_iter-1)) )
        nimastack = len(plist)
    else:
        plist = 0
        nimastack = 0

    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    nimastack = spx.bcast_number_to_all(nimastack, source_node = Blockdata["main_node"], mpi_comm=mpi.MPI_COMM_WORLD)

    # Bcast plist to all zero CPUs
    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)
    
    if(Blockdata["subgroup_myid"] > -1 ):
        # First check number of nodes, if only one, no reduction necessary.
        #print  "  subgroup_myid   ",Blockdata["subgroup_myid"],Blockdata["no_of_groups"],nimastack
        if(Blockdata["no_of_groups"] > 1):          
            plist = bcast_list_to_all(plist, Blockdata["subgroup_myid"], source_node = 0, mpi_comm = Blockdata["subgroup_comm"])
    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    # reserve buffers
    disp_unit = np.dtype("f4").itemsize
    size_of_one_image = target_nx*target_nx
    orgsize = nimastack*size_of_one_image #  This is number of projections to be computed simultaneously times their size

    if( Blockdata["myid_on_node"] == 0 ): 
        size = orgsize
    else:  
        size = 0

    win_sm, base_ptr = mpi.mpi_win_allocate_shared( size*disp_unit, disp_unit, mpi.MPI_INFO_NULL, Blockdata["shared_comm"])
    size = orgsize

    if( Blockdata["myid_on_node"] != 0 ):
        base_ptr, = mpi.mpi_win_shared_query(win_sm, mpi.MPI_PROC_NULL)

    buffer = np.frombuffer(np.core.multiarray.int_asbuffer(base_ptr, size*disp_unit), dtype = 'f4')
    buffer = buffer.reshape(nimastack, target_nx, target_nx)

    emnumpy2 = spx.EMNumPy()
    bigbuffer = emnumpy2.register_numpy_to_emdata(buffer)

    #  read data on process 0 of each node
    if( Blockdata["myid_on_node"] == 0 ):
        for i in range(nimastack):
            bigbuffer.insert_clip( spx.get_im(Blockdata["stack_ali2d"],plist[i]),(0,0,i) ) # <-- this is the way
        del plist

    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)
    alldata = [None]*nimastack
    emnumpy3 = [None]*nimastack

    msk = spx.model_blank(target_nx, target_nx,1,1)
    for i in range(nimastack):
        pointer_location = base_ptr + i*size_of_one_image*disp_unit
        img_buffer  = np.frombuffer(np.core.multiarray.int_asbuffer(pointer_location, size_of_one_image*disp_unit), dtype = 'f4')
        img_buffer  = img_buffer.reshape(target_nx, target_nx)
        emnumpy3[i] = spx.EMNumPy()
        alldata[i]  = emnumpy3[i].register_numpy_to_emdata(img_buffer)

    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    if( Blockdata["myid"] == 0 ):
        print("*************************************************************************************")
        print("     Main iteration: %3d,  Generation: %3d. "%(main_iter,generation_iter)+"   "+time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime()))   

    ave, all_params = iter_isac_pap(
        alldata,
        options.ir, target_radius, options.rs,
        target_xr, target_yr, options.ts,
        options.maxit, False, 1.0, options.dst,  # NOTE: CTF=False
        options.FL, options.FH, options.FF,
        options.init_iter, options.iter_reali,
        options.stab_ali, options.thld_err,
        options.img_per_grp, generation_iter,
        random_seed=options.rand_seed, new=False)

    #  Clean the stack
    mpi.mpi_win_free(win_sm)
    emnumpy2.unregister_numpy_from_emdata()
    del emnumpy2
    for i in range(nimastack):  
        emnumpy3[i].unregister_numpy_from_emdata()
    del alldata

    if( Blockdata["myid"] == Blockdata["main_node"] ):
        #  How many averages alreay exist
        if( os.path.exists(os.path.join(Blockdata["masterdir"],"class_averages.hdf")) ):
            nave_exist = global_def.EMUtil.get_image_count(os.path.join(Blockdata["masterdir"],"class_averages.hdf"))
        else: nave_exist = 0
        #  Read all parameters table from masterdir
        all_parameters = spx.read_text_row( os.path.join(Blockdata["masterdir"],"all_parameters.txt"))
        plist = spx.read_text_file(os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, \
            "generation%03d"%(generation_iter-1), "to_process_next_%03d_%03d.txt"%(main_iter,generation_iter-1)) )
        #print "****************************************************************************************************",os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, \
        #   "generation%03d"%(generation_iter-1), "to_process_next_%03d_%03d.txt"%(main_iter,generation_iter-1))
        j = 0
        good = []
        bad = []
        for i,q in enumerate(ave):
            #  Convert local numbering to absolute numbering of images
            local_members = q.get_attr("members")
            members = [plist[l] for l in local_members]
            q.write_image(os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter,"original_class_averages_%03d_%03d.hdf"%(main_iter,generation_iter)),i)
            q.set_attr("members",members)
            q.write_image(os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter,"class_averages_%03d_%03d.hdf"%(main_iter,generation_iter)),i)
            if(len(members)> options.minimum_grp_size):
                good += members
                q.write_image(os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter,"good_class_averages_%03d_%03d.hdf"%(main_iter,generation_iter)),j)
                q.write_image(os.path.join(Blockdata["masterdir"],"class_averages.hdf"),j+nave_exist)
                j += 1

                # We have to update all parameters table
                for l,m in enumerate(members):
                    #  I had to remove it as in case of restart there will be conflicts
                    #if( all_parameters[m][-1] > -1):
                    #   print "  CONFLICT !!!"
                    #   exit()
                    all_parameters[m] = all_params[local_members[l]]
            else:
                bad += members

        if(len(good)> 0):
            spx.write_text_row( all_parameters, os.path.join(Blockdata["masterdir"],"all_parameters.txt"))
            good.sort()
            #  Add currently assigned images to the overall list
            if( os.path.exists( os.path.join(Blockdata["masterdir"], "processed_images.txt") ) ):
                lprocessed = good + spx.read_text_file(os.path.join(Blockdata["masterdir"], "processed_images.txt" ))
                lprocessed.sort()
                spx.write_text_file(lprocessed, os.path.join(Blockdata["masterdir"], "processed_images.txt" ))
            else:
                spx.write_text_file(good, os.path.join(Blockdata["masterdir"], "processed_images.txt" ))
        
        if(len(bad)> 0):
            bad.sort()
            spx.write_text_file(bad, os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, \
                "generation%03d"%(generation_iter), "to_process_next_%03d_%03d.txt"%(main_iter,generation_iter)) )
            
            if( (int(len(bad)*1.2) < 2*options.img_per_grp) or ( (len(good) == 0) and (generation_iter == 1) ) ):
                #  Insufficient number of images to keep processing bad set
                #    or 
                #  Program cannot produce any good averages from what is left at the beginning of new main
                try:  lprocessed = spx.read_text_file(os.path.join(Blockdata["masterdir"], "processed_images.txt" ))
                except:  lprocessed = []
                nprocessed = len(lprocessed)
                leftout = sorted(list(set(range(Blockdata["total_nima"])) - set(lprocessed)))
                spx.write_text_file(leftout, os.path.join(Blockdata["masterdir"], "not_processed_images.txt" ))
                # Check whether what remains can be still processed in a new main interation
                if( ( len(leftout) < 2*options.img_per_grp) or ( (len(good) == 0) and (generation_iter == 1) ) ):
                    #    if the the number of remaining all bad too low full stop
                    keepdoing_main = False
                    keepdoing_generation = False
                    cmd = "{} {}".format("touch", os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter, "finished"))
                    junk = spx.cmdexecute(cmd)
                    cmd = "{} {}".format("touch", os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "finished"))
                    junk = spx.cmdexecute(cmd)
                    cmd = "{} {}".format("touch", os.path.join(Blockdata["masterdir"], "finished"))
                    junk = spx.cmdexecute(cmd)
                    print("*         There are no more images to form averages, program finishes     "+time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime())+"     *")
                else:
                    #  Will have to increase main, which means putting all bad left as new good, 
                    keepdoing_main = True
                    keepdoing_generation = False
                    #  Will have to increase main, which means putting all bad left as new good, 
                    cmd = "{} {}".format("touch", os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter, "finished"))
                    junk = spx.cmdexecute(cmd)
                    cmd = "{} {}".format("touch", os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "finished"))
                    junk = spx.cmdexecute(cmd)
            else:
                keepdoing_main = True
                keepdoing_generation = True
        else:
            keepdoing_main = False
            keepdoing_generation = False

    else:
        keepdoing_main = False
        keepdoing_generation = False
        
    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    keepdoing_main = spx.bcast_number_to_all(keepdoing_main, source_node = Blockdata["main_node"], mpi_comm = mpi.MPI_COMM_WORLD)
    keepdoing_generation = spx.bcast_number_to_all(keepdoing_generation, source_node = Blockdata["main_node"], mpi_comm = mpi.MPI_COMM_WORLD)

    return keepdoing_main, keepdoing_generation

#================================================================[ parameters ]

def parse_parameters( prog_name, usage, args ):

    prog_name = os.path.basename( prog_name )
    parser = OptionParser( usage, version=spx.SPARXVERSION )

    # ISAC command line parameters (public)
    parser.add_option( "--radius",           type="int",                         help="[ MANDATORY ] Particle radius in pixel." )
    parser.add_option( "--target_radius",    type="int",          default=29,    help="Target particle radius: Actual particle radius on which GPU ISAC will process data. Images will be shrunken/enlarged to achieve this radius. (Default 29)" )
    parser.add_option( "--target_nx",        type="int",          default=76,    help="Target particle image size: Actual image size on which isac will process data. Images will be shrunken/enlarged according to target particle radius and then cut/padded to achieve target_nx size. When xr > 0, the final image size for isac processing is 'target_nx + xr - 1'. (Default 76)" )
    parser.add_option( "--img_per_grp",      type="int",          default=200,   help="Number of images per class; also defines number of classes K=(total number of images)/img_per_grp. (Default 200)" )
    parser.add_option( "--minimum_grp_size", type="int",          default=-1,    help="Minimum class size. If not specified, this value will be set to 60% of the 'img_per_group' value (see above)." )
    parser.add_option( "--CTF",              action="store_true", default=False, help="If set the data will be phase-flipped using the CTF information included in image headers. (Default false)" )
    parser.add_option( "--VPP",              action="store_true", default=False, help="Set this flag when processing phase plate data. (Default false)" )
    parser.add_option( "--ir",               type="int",          default=1,     help="Inner ring radius (in pixel) of the resampling to polar coordinates. (Default 1)" )
    parser.add_option( "--rs",               type="int",          default=1,     help="Ring step (in pixel) of the resampling polar coordinates. (Default 1)" )
    parser.add_option( "--xr",               type="int",          default=1,     help="x range of the translational search during stability test alignments. This will be set by GPU ISAC. (Default 1)" )
    parser.add_option( "--yr",               type="int",          default=-1,    help="y range of translational search during stability test alignments. If this is not the same as 'xr', (GPU) ISAC will explode. (Default -1)" )
    parser.add_option( "--ts",               type="float",        default=1.0,   help="Search step of translational search during alignment. (Default 1.0)" )
    parser.add_option( "--maxit",            type="int",          default=30,    help="Number of iterations for reference-free alignment during the stability test iterations. (Default 30)" )
    parser.add_option( "--center_method",    type="int",          default=0,     help="Method for centering of global 2D averages during the initial prealignment of the data (0 : average centering; -1 : average shift method; please see center_2D in utilities.py for methods 1-7). (Default 0)" )
    parser.add_option( "--dst",              type="float",        default=90.0,  help="Discrete angle used in within group alignment. (Default 90.0)" )
    
    parser.add_option( "--FL",               type="float",        default=0.2,   help="Lowest stopband: Frequency used in the tangent filter; needs to be within [0.0, 0.5]. (Default 0.2)" )
    parser.add_option( "--FH",               type="float",        default=0.45,  help="Highest stopband: Frequency used in the tangent filter; needs to be within [0.0, 0.5]. (Default 0.45)" )
    parser.add_option( "--FF",               type="float",        default=0.2,   help="Fall-off of the tangent filter; lower values indicate a steeper transition. (Default 0.2)" )
    
    parser.add_option( "--init_iter",        type="int",          default=7,     help="Maximum number of generation iterations performed for a given subset. (Default 7)" )
    parser.add_option( "--iter_reali",       type="int",          default=1,     help="SAC stability check interval: every iter_reali iterations of SAC stability checking is performed. (Default 1)" )
    parser.add_option( "--stab_ali",         type="int",          default=5,     help="Number of alignments when checking stability. (Default 5)" )
    parser.add_option( "--thld_err",         type="float",        default=0.7,   help="Threshold of pixel error when checking stability. Equals root mean square of distances between corresponding pixels from set of found transformations and their average transformation; depends linearly on square of radius (parameter target_radius). units - pixels. (Default 0.7)" )
    parser.add_option( "--restart",          type="int",          default='-1',  help="0: restart ISAC2 after last completed main iteration (meaning there is file >finished< in it.  k: restart ISAC2 after k'th main iteration (It has to be completed, meaning there is file >finished< in it. Higer iterations will be removed.)  Default: no restart" )
    parser.add_option( "--rand_seed",        type="int",                         help="Random seed set before calculations: useful for testing purposes. By default, total randomness (type int)" )

    parser.add_option( "--filament_width",       type="int",          default=-1,    help="When this is set to a non-default value helical data is assumed in which case particle images will be masked with a rectangular mask. (Default: -1)" )
    parser.add_option( "--filament_mask_ignore", action="store_true", default=False, help="Only relevant when parameter '--filament_width' is set. When set to False a rectangular mask is used to (a) normalize and (b) to mask the particle images. The latter can be disabled by setting this flag to True. (Default: False)" )

    parser.add_option( "--main_iter_limit",  type="int",          default=-1,    help="If set to a non-zero value N, ISAC execution is halted after N main iterations. [Default: -1]" )

    # developer parameters (not listed in docs)
    parser.add_option("--skip_prealignment", action="store_true", default=False, help="Skip pre-alignment step; use if images are already centered. if set, 2D alignment directory will still be generated but the parameters will be zero. [Default: False]")

    # GPU parameters
    parser.add_option( "--gpu_devices",      type="string",       default="",    help="Specify the GPUs to be used (e.g. --gpu_devices=0, or --gpu_devices=0,1 for one or two GPUs, respectively). Using \"$ nividia-smi\" in the terminal will print out what GPUs are available. For a more detailed printout you can also use --gpu_info here in ISAC. [Default: None]" )
    parser.add_option( "--gpu_info",         action="store_true", default=False, help="Print detailed information about the selected GPUs, including the class limit which is relevant when using the --gpu_class_limit parameter. Use --gpu_devices to specify what GPUs you want to know about. NOTE: ISAC will stop after printing this information, so don't use this parameter if you intend to actually process any data. [Default: False]" )
    parser.add_option( "--gpu_memory_use",   type="float",        default=0.9,   help="Specify how much memory on the chosen GPUs ISAC is allowed to use. A value of 0.9 results in using 90% of the available memory (this is the default; higher percentages should be used with caution). [Default: 0.9]" )

    # dev parameters
    parser.add_option( "--hop_like_a_bunny", action="store_true", default=False, help="Set this flage to hop like a bunny. Until clustering runs on the GPU, this can be used to only run a single clustering iteration instead of 5. Allows fast screening of the data. [Default: False (until clustering runs on the GPU)]" )

    # trigger help manually (OptionParser and MPI aren't friends it seems)
    for arg in args:
        if arg =="-h" or arg=="--h" or arg=="-help" or arg=="--help":
            if mpi.mpi_comm_rank(mpi.MPI_COMM_WORLD)==0: parser.print_help()      # print help manually
            mpi.mpi_barrier(mpi.MPI_COMM_WORLD); mpi.mpi_finalize(); sys.exit();  # clean MPI exit

    return parser.parse_args(args)

#======================================================================[ main ]

def main(args):

    #------------------------------------------------------[ command line parameters ]

    usage = ( sys.argv[0] + " stack_file  [output_directory] --radius=particle_radius" 
              + " --img_per_grp=img_per_grp --CTF <The remaining parameters are" 
              + " optional --ir=ir --rs=rs --xr=xr --yr=yr --ts=ts --maxit=maxit"
              + " --dst=dst --FL=FL --FH=FH --FF=FF --init_iter=init_iter" 
              + " --iter_reali=iter_reali --stab_ali=stab_ali --thld_err=thld_err"
              + " --rand_seed=rand_seed>" )

    options, args = parse_parameters( sys.argv[0], usage, args )  # NOTE: output <args> != input <args>

    # after parsing, the only remaining args should be path to input & output folders
    if len(args) > 2:
        print("usage: " + usage)
        print("Please run '" +  sys.argv[0] + " -h' for detailed options")
        sys.exit()
    elif( len(args) == 2):
        Blockdata["stack"]  = args[0]
        Blockdata["masterdir"] = args[1]
    elif( len(args) == 1):
        Blockdata["stack"]  = args[0]
        Blockdata["masterdir"] = ""

    # global cache setting to allow .bdb file reading from multiple nodes
    if global_def.CACHE_DISABLE: spx.disable_bdb_cache()

    #    ██████╗  ██████╗  ██╗   ██╗      ██╗ ███████╗  █████╗   ██████╗   #
    #   ██╔════╝  ██╔══██╗ ██║   ██║      ██║ ██╔════╝ ██╔══██╗ ██╔════╝   #
    #   ██║  ███╗ ██████╔╝ ██║   ██║      ██║ ███████╗ ███████║ ██║        #
    #   ██║   ██║ ██╔═══╝  ██║   ██║      ██║ ╚════██║ ██╔══██║ ██║        #
    #   ╚██████╔╝ ██║      ╚██████╔╝      ██║ ███████║ ██║  ██║ ╚██████╗   #
    #    ╚═════╝  ╚═╝       ╚═════╝       ╚═╝ ╚══════╝ ╚═╝  ╚═╝  ╚═════╝   #

    if Blockdata["myid"] == 0:
        print_splash()
        print( "\n(SPHIRE)" ); Util.version();
        print( "   Running GPU ISAC command:\n   $ " + "\n     ".join([a for a in sys.argv]) + "\n" )

    tmp_img = EMData() # this is just a placeholder EMData object that we'll re-use in a couple of loops

    # check required options
    required_option_list = ['radius']

    for required_option in required_option_list:
        if not options.__dict__[required_option]:
            print( "\n ==%s== mandatory option is missing.\n" % required_option )
            print( "Please run '" +  sys.argv[0] + " -h' for detailed options" )
            return 1
    
    # set minimum group size if it wasn't specified
    if options.minimum_grp_size == -1:
        options.minimum_grp_size = int( options.img_per_grp*0.6 )
        if Blockdata["myid"]==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Using automatically determined minimum_grp_size of "+str(options.minimum_grp_size)+" ." )

    # sanity check: make sure the minimum group size is smaller than the actual group size
    if options.minimum_grp_size > options.img_per_grp:
        if Blockdata["myid"] == Blockdata["main_node"]:
            print( "\nERROR! Minimum group size (" + str(options.minimum_grp_size) + ") is larger than the actual group size (" + str(options.img_per_grp) + "). Oh dear :(\n" )
        return 1

    # sanity check: make sure tangent filter options are within valid ranges
    mpi_assert( options.FL >= 0.0 and options.FL <= 0.5, "ERROR! FL="+str(options.FL)+" outside valid range [0.0, 0.5]." )
    mpi_assert( options.FH >= 0.0 and options.FH <= 0.5, "ERROR! FH="+str(options.FH)+" outside valid range [0.0, 0.5]." )
    mpi_assert( options.FF >= 0.0 and options.FF <= 0.5, "ERROR! FF="+str(options.FF)+" outside valid range [0.0, 0.5]." )

    # sanity check: make sure main iteration limit makes sense
    mpi_assert( options.main_iter_limit==-1 or options.main_iter_limit>0, "ERROR! Value "+str(options.main_iter_limit)+" for main iteration limit (--main_iter_limit) parameter makes no sense, should be at least 1. Sadness :(" )

    # sanity check: make sure we're using the right centering method
    if options.center_method == -1:
        print("\n\nNO! (ISAC default should be to use centering=0\n\n")

    #------------------------------------------------------[ master directory setup ]

    if Blockdata["myid"]==0:
        print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Parameter sanity checks passed" )
        print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Create master directory" )

    # get mpi id values (NOTE: the code cannot decide which one of these to use)
    main_node = Blockdata["main_node"]
    myid  = Blockdata["myid"]
    nproc = Blockdata["nproc"]

    # main process creates the master directory
    str_len = 0
    if Blockdata["myid"] == Blockdata["main_node"]:

        # no master directory name given
        if Blockdata["masterdir"] == "":
            Blockdata["masterdir"] = "ISAC_run_" + time.strftime( "%b-%d-%Y_%H:%M:%S", time.localtime() )
            str_len = len( Blockdata["masterdir"] )
            spx.cmdexecute( "mkdir " + Blockdata["masterdir"] )
            print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: No master directory was specified. Using automatically generated directory \'" + Blockdata["masterdir"] + "\'.\n" )

        elif not os.path.exists(Blockdata["masterdir"]):
            spx.cmdexecute( "mkdir " + Blockdata["masterdir"] )

    mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

    # if no master directory name was given, the main process creates one (above) and passes it along
    str_len = mpi.mpi_bcast( str_len, 1, mpi.MPI_INT, Blockdata["main_node"], mpi.MPI_COMM_WORLD)[0]
    if( str_len > 0 ):
        Blockdata["masterdir"] = mpi.mpi_bcast( Blockdata["masterdir"], str_len, mpi.MPI_CHAR, Blockdata["main_node"], mpi.MPI_COMM_WORLD )
        Blockdata["masterdir"] = "".join( Blockdata["masterdir"] ) # mpi_bcast() returns list of characters

    # add stack_ali2d path to blockdata
    Blockdata["stack_ali2d"] = "bdb:" + os.path.join(Blockdata["masterdir"], "stack_ali2d" )
    
    if(myid == main_node): print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Master directory: \'" + Blockdata["masterdir"] + "\'" )
    mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

    #------------------------------------------------------[ zero group ]

    create_zero_group()  # group of per-node main processes with local/node id zero

    #------------------------------------------------------[ gather image parameters ]

    target_nx  = options.target_nx
    target_xr  = options.xr
    target_nx += target_xr - 1

    if (options.yr == -1):
        target_yr = options.xr
    else: 
        target_yr = options.yr

    shrink_ratio = float(options.target_radius) / float(options.radius)

    # sanity checks
    if options.CTF and options.VPP:
        ERROR( "Options CTF and VPP cannot be used together", "isac2", 1, myid )

    if( options.radius < 1 ):
        ERROR( "Particle radius has to be provided!", "sxisac", 1, myid )

    mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

    # get total number of images (nima) and broadcast
    if(myid == main_node): 
        Blockdata["total_nima"] = global_def.EMUtil.get_image_count(Blockdata["stack"])
    else: 
        Blockdata["total_nima"] = 0

    Blockdata["total_nima"] = spx.bcast_number_to_all(Blockdata["total_nima"], source_node = main_node)

    mpi_assert( options.img_per_grp <= Blockdata["total_nima"], "ERROR! %d particles are not enough particles to fill a single class (size %d)." % (Blockdata["total_nima"], options.img_per_grp) )

    #------------------------------------------------------[ GPU prep ]

    # tmp option to allow for fast screening w/o GPU alignment in clustering
    Blockdata["hop_like_a_bunny"] = options.hop_like_a_bunny
    if myid==0 and Blockdata["hop_like_a_bunny"]==True:
        print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: GPU ISAC hop like a bunny.")

    # map our GPU selection to list of available GPUs
    os.environ["CUDA_VISIBLE_DEVICES"] = options.gpu_devices
    options.gpu_devices = ",".join( map(str, range(len(options.gpu_devices.split(",")))) )

    # available gpu devices
    global GPU_DEVICES
    if options.gpu_devices != "":
        GPU_DEVICES = map( int, options.gpu_devices.split(",") )
        if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Using CUDA devices", GPU_DEVICES )
    else:
        GPU_DEVICES = [0]
        if myid==0: 
            print( "\nWarning: No GPU was specified. Using GPU [0] by default.")
            print( "           -> Program will crash if the selected GPU does not suffice." )
            print( "           -> Use \"$ nividia-smi\" in the terminal to see a list of available GPUs.\'\n" )

    # sanity check: make sure each GPU can be assigned to an MPI process
    mpi_assert( len(GPU_DEVICES) <= Blockdata["nproc"], "ERROR! Trying to use more GPUs (x"+str(len(GPU_DEVICES))+") than MPI are available (x"+str(Blockdata["nproc"])+")!" )

    # GPU memory use
    if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Memory use per device: %.2f" %(options.gpu_memory_use) )

    # GPU info
    if options.gpu_info:
        if myid==0:
            for cuda_device_id in GPU_DEVICES:
                print( "\n____[ CUDA device("+str(cuda_device_id)+") ]____" )
                applications.print_gpu_info(cuda_device_id)
                print( "____[ Class limit ]____" )
                print( "computing..", end="" )
                sys.stdout.flush()
                t = time.time()
                l = applications.multigroup_refinement_gpu_fit_max(
                    (Blockdata["total_nima"]//options.minimum_grp_size+1)//(len(GPU_DEVICES)+1),
                    options.img_per_grp,
                    options.target_nx,
                    options.target_radius,
                    target_xr, target_yr,
                    cuda_device_id=myid,
                    cuda_device_occ=options.gpu_memory_use,
                    verbose=True)
                class_limit = l[1]
                print( "\rUsing current ISAC parameters CUDA device("+str(cuda_device_id)+") holds "+str(class_limit)+" classes (%.2fs)." % (time.time()-t) )
                print( "(Parameters influencing how many ISAC classes can fit into GPU memory:              )" )
                print( "(img_per_group, target_nx, target_radius, target_xr, target_yr, and gpu_memory_use. )\n" )
        mpi.mpi_finalize()
        sys.exit()

    # GPU class limit (memtest)
    global GPU_CLASS_LIMIT
    if myid==0:
        print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Determining available GPU class load (NOTE: This assumes all used GPUs are identical!)" )
        t = time.time()
        l = applications.multigroup_refinement_gpu_fit_max(
            (Blockdata["total_nima"]//options.minimum_grp_size+1)//(len(GPU_DEVICES)+1),
            options.img_per_grp,
            options.target_nx,
            options.target_radius,
            target_xr, target_yr,
            cuda_device_id=myid,
            cuda_device_occ=options.gpu_memory_use,
            verbose=False)
        GPU_CLASS_LIMIT = l[1]
        print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Each GPU holds "+str(GPU_CLASS_LIMIT)+" classes (%.2fs) for stability testing." % (time.time()-t) )

    GPU_CLASS_LIMIT = mpi.mpi_bcast( GPU_CLASS_LIMIT, 1, mpi.MPI_INT, 0, mpi.MPI_COMM_WORLD )[0]

    # GPU process communicator
    global MPI_GPU_COMM
    MPI_GPU_COMM = mpi.mpi_comm_split( mpi.MPI_COMM_WORLD, (Blockdata["myid_on_node"] in GPU_DEVICES), myid )

    #------------------------------------------------------[ Memory check ]

    # percentage of system memory we allow ourselves to occupy; we leave some
    # in over to leave gpu isac and others some breathing room
    sys_mem_use = 0.75

    # we use a linux system call to get the RAM info we need
    if "linux" in sys.platform:
        if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Running memory check.." )

        # get amount of available system RAM
        sys_mem_avl = float( os.popen("free").readlines()[1].split()[6] ) / 1024 # mem size in MB (free returns kb)
        if myid==0:
            print( " "*32, "..avl sys mem (RAM): %.2f MB / %.2f GB" % (sys_mem_avl, sys_mem_avl/1024) )
            print( " "*32, "..use sys mem (RAM): %.2f MB / %.2f GB" % (sys_mem_avl*sys_mem_use, (sys_mem_avl*sys_mem_use)/1024) )

        # estimate memory requirement of raw data
        tmp_img.read_image( Blockdata["stack"], 0 )
        data_size_raw = (Blockdata["total_nima"]*tmp_img.get_xsize()*tmp_img.get_ysize()*4.) / 1024**2 # mem size in MB
        if myid==0: print( " "*32, "..est mem req (raw data, %d %dx%d images): %.2f MB / %.2f GB" %
            (Blockdata["total_nima"], tmp_img.get_xsize(), tmp_img.get_ysize(),
             data_size_raw, data_size_raw/1024) )

        # estimate memory requirement of downsampled data
        tmp_img = resample( tmp_img, shrink_ratio )
        data_size_sub = (Blockdata["total_nima"]*tmp_img.get_xsize()*tmp_img.get_ysize()*4.) / 1024**2 # mem size in MB
        if myid==0: print( " "*32, "..est mem req (downsampled data, %d %dx%d images): %.2f MB / %.2f GB" %
            (Blockdata["total_nima"], tmp_img.get_xsize(), tmp_img.get_ysize(),
             data_size_sub, data_size_sub/1024) )

        # batch size of input reads per MPI proc
        batch_mem_avl = (sys_mem_avl*sys_mem_use - data_size_sub) / Blockdata["nproc"]
        batch_img_num = int( batch_mem_avl / (data_size_raw/Blockdata["total_nima"]) )
        if myid==0: print( " "*32, "..%d MPI procs set to read data in batches of (max) %d images each (batch mem: %.2f MB / %.2f GB)" %
              (Blockdata["nproc"], batch_img_num, batch_mem_avl, batch_mem_avl/1024) )
        mpi_assert( batch_img_num > 0, "Memory cannot even! batch_img_num is %d"%batch_img_num )

        # make sure we can keep the downsampled data in RAM
        if data_size_sub > sys_mem_avl*sys_mem_use and data_size_sub < sys_mem_avl:
            if myid==0: print(" "*32, ">>WARNING. Requested job requires almost all available system RAM." )
        elif data_size_sub > sys_mem_avl:
            if myid==0: print(" "*32, ">>ERROR. Requested job will not fit into available system RAM!" )
        else:
            if myid==0: print(" "*32, ">>All good to go!" )
    else:
        if myid==0: print( "WARNING! Running on unsupported platform. No memory check was performed." )

    #------------------------------------------------------[ initial 2D alignment (centering) ]

    init2dir = os.path.join(Blockdata["masterdir"],"2dalignment")

    if not checkitem(os.path.join(init2dir, "Finished_initial_2d_alignment.txt")):

        if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Pre-alignment not yet done. Setting up.." )

        #  Create output directory
        if(myid == 0):
            log2d = Logger(BaseLogger_Files())
            log2d.prefix = os.path.join(init2dir)
            cmd = "mkdir -p "+log2d.prefix
            outcome = subprocess.call(cmd, shell=True)
            log2d.prefix += "/"

        else:
            outcome = 0
            log2d = None

        # make extra double sure the file system has caught up
        mpi.mpi_barrier( mpi.MPI_COMM_WORLD )
        while not os.path.exists(os.path.join(init2dir)+"/"):
            time.sleep(1)
        mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

        #--------------------------------------------------[ first data read ]

        if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Reading data" )

        # local setup
        image_start, image_end = spx.MPI_start_end( Blockdata["total_nima"], Blockdata["nproc"], myid )
        original_images = [None] * (image_end-image_start)

        # all MPI procs: compute the rotational power spectrum for VPP so we can apply it during the read if needed
        if options.VPP:
            tmp_img.read_image(Blockdata["stack"], 0)
            ntp = len( spx.rops_table(tmp_img) )
            rpw = [0.0]*ntp

            for img_idx in range(image_start, image_end):
                tmp_img.read_image(Blockdata["stack"], img_idx)
                tpw = spx.rops_table( tmp_img )
                for i in range(ntp):
                    rpw[i] += np.sqrt(tpw[i])

            rpw = mpi.mpi_reduce( rpw, ntp, mpi.MPI_FLOAT, mpi.MPI_SUM, main_node, mpi.MPI_COMM_WORLD )

            if myid==0:
                rpw = [float(Blockdata["total_nima"]/q) for q in rpw]
                rpw[0] = 1.0
                spx.write_text_file(rpw,os.path.join(Blockdata["masterdir"], "rpw.txt"))
            else:
                rpw = []

            rpw = spx.bcast_list_to_all(rpw, myid, source_node = main_node, mpi_comm=mpi.MPI_COMM_WORLD)

        else:
            if myid==0:
                tmp_img.read_image(Blockdata["stack"], 0)
                ntp = len( spx.rops_table(tmp_img) )
                spx.write_text_file( [0.0]*ntp,os.path.join(Blockdata["masterdir"], "rpw.txt") )

        #--------------------------------------------------[ skip the pre-alignment ]

        if options.skip_prealignment:
            if myid==0:
                print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Skipping pre-alignment. \'%s\' parameters are set to zero" % os.path.join(init2dir, "initial2Dparams.txt") )
                params2d = [[0.0,0.0,0.0,0] for i in range(0, Blockdata["total_nima"])]
                spx.write_text_row( params2d, os.path.join(init2dir,"initial2Dparams.txt") )

        #--------------------------------------------------[ read & distribute data for the pre-alignment ]

        else:

            # all MPI procs: read and immediately process/resample data batches
            idx=0
            batch_start = image_start
            while batch_start < image_end:
                batch_end = min( batch_start+batch_img_num, image_end )

                # read batch and process
                for i, img_idx in enumerate( range(batch_start, batch_end) ):
                    tmp_img.read_image( Blockdata["stack"], img_idx )

                    if options.VPP:
                        tmp_img = spx.filt_table( tmp_img, rpw )

                    if options.CTF:
                        tmp_img = filt_ctf( tmp_img, tmp_img.get_attr("ctf"), binary=True )

                    original_images[idx+i] = resample( tmp_img, shrink_ratio )

                    print_progress("BATCHREAD/PREP%s][PROC%s" % (str(int(idx/batch_img_num)).zfill(2), str(myid).zfill(2)), i, batch_end-batch_start)
                print("")

                # go to next batch
                batch_start += batch_img_num
                idx += batch_img_num

            mpi.mpi_barrier( mpi.MPI_COMM_WORLD ) # just to print msg below after the progress bars above
            if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Distributing workload to available GPUs" )

            #--------------------------------------------------[ collect data from all procs in GPU procs ]

            # step 01: everyone sends their data to the GPU looking for it
            for gpu in GPU_DEVICES:
                gpu_img_start, gpu_img_end = spx.MPI_start_end( Blockdata["total_nima"], len(GPU_DEVICES), gpu )

                if gpu==myid:
                    continue

                for i, my_img in enumerate( range(image_start, image_end) ):
                    if gpu_img_start <= my_img < gpu_img_end:
                        spx.send_EMData( original_images[i], gpu, my_img, comm=mpi.MPI_COMM_WORLD )
                        original_images[i] = None

            original_images = [ i for i in original_images if i is not None ] # might be a GPU process sends some and keeps the rest

            # step 02a: each GPU proc receives the desired data as offered by the other processes
            if myid in GPU_DEVICES:
                image_start, image_end = spx.MPI_start_end( Blockdata["total_nima"], len(GPU_DEVICES), myid )

                for proc in range( Blockdata["nproc"] ):
                    proc_img_start, proc_img_end = spx.MPI_start_end( Blockdata["total_nima"], Blockdata["nproc"], proc )

                    if proc==myid:
                        continue

                    for proc_img in range( proc_img_start, proc_img_end ):
                        if image_start <= proc_img < image_end:
                            original_images.append( spx.recv_EMData(proc, proc_img, comm=mpi.MPI_COMM_WORLD) )

            # step 2b: each non-GPU proc makes sure they sent off all their data
            else:
                assert len(original_images) == 0, "ERROR: proc[%d] still holds %d images." % (myid, len(original_images))
                image_start, image_end = None, None

            mpi.mpi_barrier(mpi.MPI_COMM_WORLD) # the above communication is blocking, but just to be sure

        #--------------------------------------------------[ run the GPU pre-alignment ]

        if Blockdata["myid_on_node"] in GPU_DEVICES and not options.skip_prealignment:

            nx   = original_images[0].get_xsize()
            txrm = (nx - 2*(options.target_radius+1)) // 2

            if(txrm < 0):
                ERROR( "ERROR!! Radius of the structure larger than the window data size permits   %d"%(options.radius), "sxisac",1, myid)

            nxrsteps = 4
            if(txrm/nxrsteps>0):
                tss = ""
                txr = ""
                while(txrm/nxrsteps>0):
                    tts=txrm/nxrsteps
                    tss += "  %d" % tts               # NOTE: This is an implicit type conversion to int!
                    txr += "  %d" % (tts*nxrsteps)
                    txrm = txrm//2
            else:
                tss = "1"
                txr = "%d"%txrm

            # 2D gpu alignment call
            if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Executing pre-alignment" )
            params2d = applications.ali2d_base_gpu_isac_CLEAN(
                original_images,                  # downsampled images handled by this process only
                init2dir,                         # output directory
                None,                             # mask
                1,                                # inner radius / first ring
                options.target_radius,            # outer radius / last ring
                1,                                # ring step
                txr,                              # list of search rangees in x-dim
                txr,                              # list of search rangees in y-dim
                tss,                              # search step size
                False,                            # no mirror flag
                90.0,                             # alignment angle reset value
                options.center_method,            # centering method (should be 0)
                14,                               # iteration limit
                options.CTF,                      # CTF flag
                1.0,                              # snr (CTF parameter)
                False,                            # some fourier flag?
                "ref_ali2d",                      # user_func_name(?)
                "",                               # randomization method
                log2d,                            # log (?)           _
                mpi.mpi_comm_size(MPI_GPU_COMM),  # mpi comm size      |
                mpi.mpi_comm_rank(MPI_GPU_COMM),  # mpi rank           |_______[ gpu communicator ]
                0,                                # mpi main proc      |
                MPI_GPU_COMM,                     # mpi communicator  _|
                write_headers=False,              # we write the align params to a file (below) but not to particle headers
                mpi_gpu_proc=(Blockdata["myid_on_node"] in GPU_DEVICES),
                cuda_device_occ=options.gpu_memory_use )

            mpi.mpi_barrier( MPI_GPU_COMM)

            if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Pre-alignment call complete" )

            for i in range(len(params2d)):
                alpha, sx, sy, mirror = spx.combine_params2(0, params2d[i][1], params2d[i][2], 0, -params2d[i][0], 0, 0, 0)
                sx /= shrink_ratio
                sy /= shrink_ratio
                params2d[i][0] = 0.0
                params2d[i][1] = sx
                params2d[i][2] = sy
                params2d[i][3] = 0

            del original_images

            mpi.mpi_barrier(MPI_GPU_COMM)

            params2d = spx.wrap_mpi_gatherv(params2d, main_node, MPI_GPU_COMM)
            if( myid == main_node ):
                spx.write_text_row( params2d, os.path.join(init2dir,"initial2Dparams.txt") )

        #--------------------------------------------------[ acquire local work load ]

        # make extra double sure the file system has caught up
        mpi.mpi_barrier(mpi.MPI_COMM_WORLD)
        while not os.path.exists(os.path.join(init2dir, "initial2Dparams.txt")):
            time.sleep(1)
        mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

        if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: Start resampling" )

        # re-distribute process load once more (note: this constitutes a gpu -> mpi parallelization transition)
        image_start, image_end = spx.MPI_start_end(Blockdata["total_nima"], nproc, myid)
        aligned_images = [None] * (image_end-image_start)

        # get alignment params for local work load
        params = spx.read_text_row(os.path.join(init2dir, "initial2Dparams.txt"))
        params = params[image_start:image_end]

        # prep defocus value correction
        if options.VPP:
            if myid == 0:
                rpw = spx.read_text_file(os.path.join(Blockdata["masterdir"], "rpw.txt"))
            else:
                rpw = [0.0]
            rpw = spx.bcast_list_to_all(rpw, myid, source_node = main_node, mpi_comm = mpi.MPI_COMM_WORLD)
            #### note: everyone could just read the file and we would not need a broadcast here

        tmp_img = EMData()
        tmp_img.read_image( Blockdata["stack"], 0 )
        mask = tmp_img.get_xsize()
        mask = spx.model_circle( options.radius, mask, mask )

        # batched read & process; only keep processed data
        idx=0
        batch_start = image_start
        while batch_start < image_end:
            batch_end = min( batch_start+batch_img_num, image_end )

            for i, img_idx in enumerate( range(batch_start, batch_end) ):
                tmp_img.read_image( Blockdata["stack"], img_idx )

                # defocus value correction
                st = Util.infomask( tmp_img, mask, False ) # st[0]: mean; st[1]: std (of area under mask)
                tmp_img -= st[0]
                if options.CTF:
                    tmp_img = filt_ctf( tmp_img, tmp_img.get_attr("ctf"), binary=True )
                elif options.VPP:
                    tmp_img = fft( spx.filt_table(filt_ctf(fft(tmp_img), tmp_img.get_attr("ctf"), binary=True), rpw) )

                # shrink and cut to size
                aligned_images[idx+i] = normalize_particle_image(
                    tmp_img,
                    shrink_ratio,
                    options.target_radius,
                    target_nx,
                    params[idx+i],
                    filament_width=options.filament_width,
                    ignore_helical_mask=options.filament_mask_ignore
                )

                print_progress( "BATCH:PREP][PROC%s"%str(myid).zfill(2), i, batch_end-batch_start )
            print("")

            # go to next batch
            batch_start += batch_img_num
            idx += batch_img_num

        if options.VPP: del rpw # don't need you anymore, get the hell out

        mpi.mpi_barrier(mpi.MPI_COMM_WORLD);

        #--------------------------------------------------[ write stack of aligned / resampled particles ]

        if myid==0: print( time.strftime("%Y-%m-%d %H:%M:%S :: ", time.localtime()) + "main() :: PROC"+str(myid)+" Writing new particle stack.. ", end="" ); sys.stdout.flush()

        # every proc adds their part to the stack_ali2d bdb-stack)
        for pid in range(Blockdata["nproc"]):
            if myid==pid:

                for loc_idx, glb_idx in enumerate(range(image_start, image_end)):
                    aligned_images[loc_idx].write_image( Blockdata["stack_ali2d"], glb_idx )
                del aligned_images

                DB = db_open_dict(Blockdata["stack_ali2d"])
                DB.close() # has to be explicitly closed

            mpi.mpi_barrier(mpi.MPI_COMM_WORLD)

        if myid==0: print( time.strftime("done (%H:%M:%S)", time.localtime()) ); sys.stdout.flush()

        # print shrink ratio information
        if( Blockdata["myid"] == main_node ):

            fp = open( os.path.join(Blockdata["masterdir"], "README_shrink_ratio.txt"), "w" )
            output_text = "            " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + """
            Since for processing purposes isac changes the image dimensions,
            adjustment of pixel size needs to be made in subsequent steps, (e.g.
            running sxviper.py). The shrink ratio and radius used for this particular isac run is
            --------
            %.5f
            %.5f
            --------
            To get the pixel size for the isac output the user needs to divide
            the original pixel size by the above value. This info is saved in
            the following file: README_shrink_ratio.txt
            """ % (shrink_ratio, options.radius)
            fp.write(output_text); fp.flush(); fp.close()
            print(output_text)
            junk = spx.cmdexecute("sxheader.py  --consecutive  --params=originalid   %s" % Blockdata["stack_ali2d"])

            fp = open(os.path.join(init2dir, "Finished_initial_2d_alignment.txt"), "w"); fp.flush(); fp.close()

    else:
        if( Blockdata["myid"] == Blockdata["main_node"] ):
            print("Skipping 2D alignment since it was already done!")

    #------------------------------------------------------[ prepare ISAC loop to run from scratch or continue ]

    error     = 0
    main_iter = 0

    if( Blockdata["myid"] == Blockdata["main_node"] ):

        # fresh start
        if( not os.path.exists( os.path.join(Blockdata["masterdir"], "main001", "generation000") ) ):
            #  NOTE: we do not create processed_images.txt selection file as it has to be initially empty
            #  we do, however, initialize all parameters with empty values
            spx.write_text_row( [[0.0,0.0,0.0,-1] for i in range(Blockdata["total_nima"])], os.path.join(Blockdata["masterdir"], "all_parameters.txt") )
            if(options.restart > -1):
                error = 1

        # continue ISAC from a previous run
        else:
            if(options.restart == 0):
                keepdoing_main = True
                main_iter = 0
                while(keepdoing_main):
                    main_iter += 1
                    if( os.path.exists( os.path.join(Blockdata["masterdir"], "main%03d"%main_iter ) ) ):
                        if( not  os.path.exists( os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "finished" ) ) ):
                            cmd = "{} {}".format( "rm -rf", 
                                                  os.path.join(Blockdata["masterdir"], "main%03d"%main_iter) )
                            junk = spx.cmdexecute(cmd)
                            keepdoing_main = False
                    else: 
                        keepdoing_main = False

            else:
                main_iter = options.restart
                if( not os.path.exists( os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "finished")) ):
                    error = 2
                else:
                    keepdoing_main = True
                    main_iter += 1
                    while( keepdoing_main ):
                        if( os.path.exists(os.path.join(Blockdata["masterdir"], "main%03d"%main_iter)) ):
                            cmd = "{} {}".format( "rm -rf", 
                                                  os.path.join(Blockdata["masterdir"], "main%03d"%main_iter) )
                            junk = spx.cmdexecute( cmd )
                            main_iter += 1
                        else: 
                            keepdoing_main = False

            if( os.path.exists(os.path.join(Blockdata["masterdir"], "finished")) ):
                cmd = "{} {}".format( "rm -rf", 
                                      os.path.join(Blockdata["masterdir"], "finished") )
                junk = spx.cmdexecute( cmd )

    error = spx.bcast_number_to_all( error, source_node = Blockdata["main_node"] )
    if( error == 1 ):
        ERROR( "isac2","cannot restart from unfinished main iteration %d" % main_iter )

    mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

    #------------------------------------------------------[ ISAC main loop ]

    keepdoing_main = True
    main_iter = 0

    while( keepdoing_main ):

        main_iter += 1

        if( checkitem(os.path.join(Blockdata["masterdir"], "finished")) ):
            keepdoing_main = False

        else:
            if( not checkitem(os.path.join(Blockdata["masterdir"], "main%03d"%main_iter )) ):
                #  CREATE masterdir
                #  Create generation000 and put files in it
                generation_iter = 0
                if( Blockdata["myid"] == 0 ):
                    cmd = "{} {}".format(
                        "mkdir", 
                        os.path.join(Blockdata["masterdir"],
                        "main%03d"%main_iter) )
                    junk = spx.cmdexecute(cmd)
                    cmd = "{} {}".format(
                        "mkdir", 
                        os.path.join(Blockdata["masterdir"],
                        "main%03d"%main_iter,
                        "generation%03d"%generation_iter) )
                    junk = spx.cmdexecute(cmd)
                    if( main_iter > 1 ):
                        #  It may be restart from unfinished main, so replace files in master
                        cmd = "{} {} {} {}".format(
                            "cp -Rp",
                            os.path.join(Blockdata["masterdir"], "main%03d"%(main_iter-1), "processed_images.txt"),
                            os.path.join(Blockdata["masterdir"], "main%03d"%(main_iter-1), "class_averages.hdf"),
                            os.path.join(Blockdata["masterdir"]) )
                        junk = spx.cmdexecute( cmd )
                        junk = os.path.join( Blockdata["masterdir"], "main%03d"%(main_iter-1), "not_processed_images.txt" )
                        if( os.path.exists(junk) ):
                            cmd = "{} {} {}".format(
                                "cp -Rp", 
                                junk, 
                                os.path.join(Blockdata["masterdir"]) )
                            junk = spx.cmdexecute( cmd )

                    if( os.path.exists( os.path.join(Blockdata["masterdir"], "not_processed_images.txt")) ):
                        cmd = "{} {} {}".format(
                                "cp -Rp", 
                                os.path.join(Blockdata["masterdir"], "not_processed_images.txt"),
                                os.path.join(Blockdata["masterdir"], 
                                    "main%03d"%main_iter, 
                                    "generation%03d"%generation_iter, 
                                    "to_process_next_%03d_%03d.txt"%(main_iter,generation_iter)) )
                        junk = spx.cmdexecute( cmd )
                    else:
                        spx.write_text_file( list(range(Blockdata["total_nima"])),
                                             os.path.join(Blockdata["masterdir"], 
                                                          "main%03d"%main_iter, 
                                                          "generation%03d"%generation_iter, 
                                                          "to_process_next_%03d_%03d.txt"%(main_iter,generation_iter)) )
                mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

            if( not checkitem(os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "finished")) ):
                keepdoing_generation = True
                generation_iter = 0
    
                while( keepdoing_generation ):
                    generation_iter += 1
                    if checkitem( os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter) ):
                        if checkitem( os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter, "finished") ):
                            okdo = False
                        else:
                            #  rm -f THIS GENERATION
                            if( Blockdata["myid"] == 0 ):
                                cmd = "{} {}".format( "rm -rf", 
                                                      os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter) )
                                junk = spx.cmdexecute( cmd )
                            mpi.mpi_barrier( mpi.MPI_COMM_WORLD )
                            okdo = True
                    else:
                        okdo = True

                    if okdo:
                        if( Blockdata["myid"] == 0 ):
                            cmd = "{} {}".format( "mkdir", 
                                                  os.path.join(Blockdata["masterdir"], "main%03d"%main_iter, "generation%03d"%generation_iter) )
                            junk = spx.cmdexecute( cmd )
                        mpi.mpi_barrier( mpi.MPI_COMM_WORLD )

                        # DO THIS GENERATION
                        keepdoing_main, keepdoing_generation = do_generation(main_iter, generation_iter, target_nx, target_xr, target_yr, options.target_radius, options)
                        # Preserve results obtained so far
                        if( not keepdoing_generation ):
                            if( Blockdata["myid"] == 0 ):
                                cmd = "{} {} {} {}".format(
                                    "cp -Rp",
                                    os.path.join(Blockdata["masterdir"], "processed_images.txt"),
                                    os.path.join(Blockdata["masterdir"], "class_averages.hdf"),
                                    os.path.join(Blockdata["masterdir"], "main%03d"%main_iter) )
                                junk = spx.cmdexecute( cmd )
                                junk = os.path.join( Blockdata["masterdir"], "not_processed_images.txt" )
                                if( os.path.exists(junk) ):
                                    cmd = "{} {} {}".format(
                                        "cp -Rp",
                                        junk,
                                        os.path.join(Blockdata["masterdir"], "main%03d"%main_iter) )
                                    junk = spx.cmdexecute( cmd )

                            ######################################## MAIN_ITER_LIMIT
                            if options.main_iter_limit >= main_iter:
                                if myid==0: print( "\n--- Specified main iteration limit reached ---\n" )
                                keepdoing_main = False
                            ######################################## MAIN_ITER_LIMIT

    mpi.mpi_barrier( mpi.MPI_COMM_WORLD )
    if( Blockdata["myid"] == 0 ):
        if( os.path.exists(os.path.join(Blockdata["masterdir"],"class_averages.hdf")) ):
            if len( EMData.read_images(os.path.join(Blockdata["masterdir"],"class_averages.hdf")) ) > 5:
                cmd = "{} {} {} {} {} {} {} {} {} {}".format(
                    "sxchains.py",
                    os.path.join(Blockdata["masterdir"],"class_averages.hdf"),
                    os.path.join(Blockdata["masterdir"],"junk.hdf"),
                    os.path.join(Blockdata["masterdir"],"ordered_class_averages.hdf"),
                    "--circular",
                    "--radius=%d"%options.target_radius ,
                    "--xr=%d"%(target_xr+1),
                    "--yr=%d"%(target_yr+1),
                    "--align",
                    ">/dev/null" )
                junk = spx.cmdexecute( cmd )
                cmd = "{} {}".format( "rm -rf",
                                      os.path.join(Blockdata["masterdir"], "junk.hdf") )
                junk = spx.cmdexecute( cmd )
            else:
                spx.cmdexecute(
                    "cp " +
                    os.path.join(Blockdata["masterdir"],"class_averages.hdf") +" "+
                    os.path.join(Blockdata["masterdir"],"ordered_class_averages.hdf") )
        else:
            print( "ISAC could not find any stable class averaging, terminating..." )

    if myid==0:
        print( "\nAll done. Good luck with your project! :)\n" )

    mpi.mpi_finalize()
    sys.exit()

if __name__=="__main__":
    main(sys.argv[1:])
