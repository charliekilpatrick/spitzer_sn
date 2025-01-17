#!/usr/bin/env python
from astropy.io import fits
from astropy.time import Time
from numpy import *
import gzip
import pickle
import sys
import tqdm
import os
import glob
import shutil

def create_mopex_cmd(basecmd, idx, channel, wd, imlist='input/images.list',
    slist='input/sigma.list', mlist='input/mask.list'):

    cmd = ''
    if '.pl' not in basecmd:
        cmd = basecmd
        cmd += '.pl'
    else:
        cmd = basecmd
        basecmd = cmd.replace('.pl','')

    ch = channel.replace('ch','I')

    if idx==0:
        cmd += f' -n {basecmd}_{ch}.nl'
    else:
        cmd += f' -n {basecmd}_{ch}_nofid.nl'

    cmd += f' -I {imlist} -S {slist} -d {mlist}'
    cmd += f' -O {wd}'

    if idx!=0:
        cmd += ' -F mosaic_fif.tbl'

    cmd += f' > {basecmd}.log'

    return(cmd)

def create_nofid_params(mopex, basecmd, channel):

    ch = channel.replace('ch','I')

    outparamfile = os.path.join(mopex, 'cdf', f'{basecmd}_{ch}_nofid.nl')
    if os.path.exists(outparamfile):
        return(None)

    paramfile = outparamfile.replace('_nofid','')

    with open(paramfile, 'r') as params:
        data = params.readlines()

    newdata = data.replace('run_fiducial_image_frame = 1',
        'run_fiducial_image_frame = 0')

    with open(outparamfile, 'w') as params:
        params.write(newdata)


def insert_into_subtractions(basedir, mopex, channel, objname,
    fake_stars,
    email='ckilpatrick@northwestern.edu'):

    out_runfiles=[]

    match = os.path.join(basedir, 'run_stacks_*.sh')
    for file in glob.glob(match):
        if os.path.exists(file):
            print(f'Deleting {file}')
            os.remove(file)

    pkl_file = os.path.join(basedir, 'fit_results.pickle')
    pkl_data = gzip.open(pkl_file, 'rb')
    [all_data, parsed, settings, SNCmat, Cmat] = pickle.load(pkl_data)

    for key in settings:
        print(f"settings: {key}")

    for key in all_data:
        print(f"all_data: {key}")

    settings["images"] = sorted(settings["images"])

    print('epoch_names:',settings['epoch_names'])
    print('epochs:',settings['epochs'])

    first_epoch_dir = ''
    for ee in sorted(settings['epoch_names']):

        wd = os.path.join(basedir, "sub_stack_epoch{0}".format(
            str(ee).zfill(3)))
        wd_im = os.path.join(wd, "ims")

        if os.path.exists(wd):
            print(f'Deleting: {wd}')
            shutil.rmtree(wd)

        os.makedirs(wd)
        os.makedirs(wd_im)

        filename = os.path.join(basedir, "run_stacks_{0}.sh".format(
            str(ee).zfill(3)))
        f_mopex = open(filename, 'w')
        f_mopex.write("sleep 2 \n")

        print(f"Figuring out which images to look at for epoch {ee}...")
        n_total = len(where(settings['epochs']==ee)[0])
        print(f"There are {n_total} candidate images")

        images_to_work_with = []
        bad_images = []
        for idx in where(settings['epochs']==ee)[0]:
            if (settings["images"][idx].count(basedir) and
                os.path.exists(settings["images"][idx])):
                images_to_work_with.append(idx)
            else:
                bad_images.append(idx)

        assert len(images_to_work_with) > 0
        n=len(images_to_work_with)
        print(f"Found {n} images to work with")
        if len(bad_images)>0:
            print(f"Bad images: {bad_images}")
        else:
            print("Found no bad images")

        first_file = settings["images"][images_to_work_with[0]]
        mjd = fits.open(first_file)[0].header['MJD_OBS']
        aorkey = str(fits.open(first_file)[0].header['AORKEY'])
        t = Time(mjd, format='mjd')
        datestr = t.datetime.strftime('ut%y%m%d')

        baseoutname = f'{objname}.{channel}.{datestr}.{aorkey}_stk.diff.fits'
        baseoutcov = baseoutname.replace('_stk.diff.fits','_cov.diff.fits')
        baseoutunc = baseoutname.replace('_stk.diff.fits','_unc.diff.fits')

        resid_file = os.path.join(basedir, 'residuals.fits')

        f = fits.open(resid_file)
        subtractions = f[0].data
        print(f"subtractions shape: {subtractions.shape}")
        f.close()

        if not os.path.exists(os.path.join(wd, 'input')):
            os.makedirs(os.path.join(wd, 'input'))

        f_ilist = open(os.path.join(wd,"input/images.list"), 'w')
        f_ulist = open(os.path.join(wd,"input/images_orig.list"), 'w')
        f_slist = open(os.path.join(wd,"input/sigma.list"), 'w')
        f_mlist = open(os.path.join(wd,"input/mask.list"), 'w')

        total_im = len(images_to_work_with)
        print(f'Writing out {total_im} images')
        for imind in images_to_work_with:
            # Construct full path to new image
            newim_base = os.path.basename(settings["images"][imind])
            unmod_base = newim_base.replace("cbcd_merged.fits", "cbcd.fits")
            newim_base = newim_base.replace("cbcd_merged.fits", "cbcd_sub.fits")
            newim = os.path.join(wd_im, newim_base)
            unmod = os.path.join(wd_im, unmod_base)

            assert newim != settings["images"][imind]

            origim_base = settings["images"][imind].replace("cbcd_merged.fits",
                "cbcd.fits")
            origim_base = os.path.basename(origim_base)
            origim_path = os.path.split(settings["images"][imind])[0]

            # Original image path should be like...
            origim_path = origim_path.replace('subtraction', channel)

            # Full path to original image
            origim = os.path.join(origim_path, origim_base)

            assert os.path.exists(origim)

            f = fits.open(origim)
            if fake_stars:
                # Need to add back in data with fake stars
                newf = fits.open(settings["images"][imind])
                f[0].data = newf['SCI'].data
                f[0].header = newf['SCI'].header
                for key in newf[0].header.keys():
                    if key not in f[0].header.keys():
                        f[0].header[key]=newf[0].header[key]
                newf.close()

            # Write image before it's modified
            f.writeto(unmod,  output_verify='silentfix', overwrite=True)

            pixels_not_modified_by_subtraction = f[0].data*0. + 1

            for i, ii in enumerate(range(all_data["pixelranges"][imind][0],
                all_data["pixelranges"][imind][1])):
                for j, jj in enumerate(range(all_data["pixelranges"][imind][2],
                    all_data["pixelranges"][imind][3])):
                    if subtractions[imind,i,j] != 0:
                        f[0].data[ii, jj] = subtractions[imind,i,j]
                        pixels_not_modified_by_subtraction[ii,jj] = 0

            sky_inds = where((pixels_not_modified_by_subtraction == 1)*\
                (1 - isnan(f[0].data))*(1 - isinf(f[0].data)))
            f[0].data -= median(f[0].data[sky_inds])*\
                pixels_not_modified_by_subtraction

            f.writeto(newim, output_verify='silentfix', overwrite=True)
            f.close()

            f_ilist.write(newim + '\n')
            f_ulist.write(unmod + '\n')
            f_slist.write(origim.replace("cbcd.fits", "cbunc.fits") + '\n')
            f_mlist.write(origim.replace("cbcd.fits", "bimsk.fits") + '\n')

        f_ilist.close()
        f_slist.close()
        f_mlist.close()

        print("This needs to run with csh")

        if int(ee)==0:
            # Should be first so first_epoch_dir will be populated
            not_first_epoch = 0
            first_epoch_dir = wd
        else:
            not_first_epoch = 1
            f_mopex.write(f"\ncp {first_epoch_dir}/mosaic_fif.tbl {wd} \n")
            create_nofid_params(mopex, 'overlap', channel)
            create_nofid_params(mopex, 'mosaic', channel)

        ch = channel.replace('ch','I')

        f_mopex.write(f"cp -r {mopex}/cal {wd} \n")
        f_mopex.write(f"cp -r {mopex}/cdf {wd} \n")
        f_mopex.write(f"cp {mopex}/mopex-script-env.csh {wd} \n")

        f_mopex.write(f"cd {wd} \n")
        f_mopex.write("source mopex-script-env.csh \n")
        f_mopex.write(create_mopex_cmd('overlap', ee, channel, wd)+' \n')
        f_mopex.write(create_mopex_cmd('mosaic', ee, channel, wd)+' \n')

        cmd1=f"mv -v Combine/mosaic.fits {baseoutname}"
        cmd2=f"mv -v Combine/mosaic_cov.fits {baseoutcov}"
        cmd3=f"mv -v Combine/mosaic_unc.fits {baseoutunc}"

        f_mopex.write(cmd1+' \n')
        f_mopex.write(cmd2+' \n')
        f_mopex.write(cmd3+' \n')

        for subdir_to_remove in ["BoxOutlier", "ReInterp", "Overlap_Corr",
            "DualOutlier", "Outlier", "Interp", "MedFilter", "Coadd", "Rmask",
            "Combine", "addkeyword.txt", "Detect", "Medfilter"]:
            f_mopex.write(f"rm -fvr {wd}/{subdir_to_remove} \n")

        # We want to redo mosaic for the original images to compare
        if fake_stars:
            baseoutname = f'{objname}.{channel}.{datestr}.{aorkey}_stk.fits'
            baseoutcov = baseoutname.replace('_stk.fits','_cov.fits')
            baseoutunc = baseoutname.replace('_stk.fits','_unc.fits')

            f_mopex.write(create_mopex_cmd('overlap', ee, channel, wd,
                imlist='input/images_orig.list')+' \n')
            f_mopex.write(create_mopex_cmd('mosaic', ee, channel, wd,
                imlist='input/images_orig.list')+' \n')

            cmd1=f"mv -v Combine/mosaic.fits {baseoutname}"
            cmd2=f"mv -v Combine/mosaic_cov.fits {baseoutcov}"
            cmd3=f"mv -v Combine/mosaic_unc.fits {baseoutunc}"

            f_mopex.write(cmd1+' \n')
            f_mopex.write(cmd2+' \n')
            f_mopex.write(cmd3+' \n')

            for subdir_to_remove in ["BoxOutlier", "ReInterp", "Overlap_Corr",
                "DualOutlier", "Outlier", "Interp", "MedFilter", "Coadd",
                "Rmask", "Combine", "cal", "cdf", "addkeyword.txt", "Detect",
                "Medfilter", "*.nl"]:
                f_mopex.write(f"rm -fvr {wd}/{subdir_to_remove} \n")

        try:
            f_mopex.close()
        except:
            print("Couldn't close file! I guess there's not one open.")

        # add to list of output run files
        print(f'Need to run {filename}')
        out_runfiles.append(filename)

    return(out_runfiles)

if __name__ == '__main__':
    # Testing the method on local machine
    insert_into_subtractions('/data/ckilpatrick/spitzer/2017gfo/ch2',
        '/data/software/mopex', 'ch2', 'AT2017gfo', True,
        email='ckilpatrick@northwestern.edu')
