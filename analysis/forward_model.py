#!/usr/bin/env python
from numpy import *
from astropy.io import fits
import multiprocessing
import sys
import os
from scipy.ndimage.interpolation import map_coordinates
from scipy.interpolate import interp2d, SmoothBivariateSpline
from scipy.stats import scoreatpercentile
from astropy import wcs
from scipy import fftpack as ft
from analysis.DavidsNM import save_img, miniLM_new, miniNM_new
import gzip
import pickle
import time
import json
import multiprocessing as mp

import warnings
warnings.filterwarnings('ignore')

def message(msg):
    print('\n\n'+msg+'\n'+'#'*80+'\n'+'#'*80+'\n\n')

# version history:
# 1.00 05-01-2018: First release
# 1.01 05-01-2018: Update to handle patches off the edge.
# 1.02 05-02-2018: added SN_centroid_prior_arcsec setting
# 1.03 05-05-2018: added fitSNoffset
# 1.04 05-24-2018: won't crash if an image has no good pixels
# 1.05 05-25-2018: Checks for NaN's in science data (not just error)
# 1.06 05-27-2018: Added apodize setting
# 1.07 06-01-2018: interp2d has a bug. Switched to SmoothBivariateSpline
# 1.08 06-03-2018: Added summary of large pulls, possibly an indicator for bad
#                  pixels. Fixed DoF bug.
# 1.09 06-04-2018: RA0, Dec0 can now be image-dependent (for when images are not
#                  aligned)
# 1.10 07-12-2018: Better derivative scales on flux
# 1.11 07-13-2018: Fixed map_coords! prefilter = True now
# 1.12 07-13-2018: Fixed map_coords for the galaxy! prefilter = True now
# 1.13 07-16-2018: Identifies SN flux in iteration
# 1.20 07-16-2018: Checks for convergence
# 1.21 07-18-2018: Added option for iterative centroiding, useful for very large
#                  numbers of dithers
# 1.30 12-14-2018: Takes one PSF for each image now.
# 1.31 12-23-2018: Doesn't use sparse Jacobian for galaxy-only fit
# 1.32 01-21-2019: Saves model of just point source
# 1.32 01-02-2019: Fixed epochs bug when starting with non-zero epoch
# 1.33 01-16-2021: Dumps json of parsed to result file
# 1.34 07-24-2022: Refactored to run inside of pipeline and streamline code
# 1.35 08-17-2022: Created forward_model class to run within pipeline
version = 1.35

def parse_line(line):
    parsed = line.split("#")[0]
    parsed = parsed.split(None)
    if len(parsed) > 1:
        parsed = [parsed[0], eval(" ".join(parsed[1:]))]
        return parsed
    else:
        return None

def robust_index(dat, i1, i2, j1, j2, fill_value = 0):
    sh = dat.shape

    paddat = zeros([sh[0] + (i2 - i1)*2, sh[1] + (j2 - j1)*2],
        dtype=dat.dtype) + fill_value
    paddat[i2 - i1: i2 - i1 + sh[0], j2 - j1: j2 - j1 + sh[1]] = dat

    return paddat[i1 + (i2 - i1):i2 + (i2 - i1),
                  j1 + (j2 - j1):j2 + (j2 - j1)]

def reshape_coeffs(coeffs, radius, return_coeffs_not_size = 1):
    reshaped = zeros([2*radius + 1, 2*radius + 1], dtype=float64)


    ind = 0
    for i in range(2*radius + 1):
        for j in range(2*radius + 1):
           if (i - radius)**2. + (j - radius)**2. < radius**2.:
               reshaped[i,j] = coeffs[ind]
               ind += 1

    if return_coeffs_not_size:
        return reshaped
    else:
        return ind

def unreshape_coeffs(coeffs, radius):
    OneD_coeffs = []

    for i in range(2*radius + 1):
        for j in range(2*radius + 1):
           if (i - radius)**2. + (j - radius)**2. < radius**2.:
               OneD_coeffs.append(coeffs[i,j])
    return array(OneD_coeffs)

def parseCmat(Cmat, settings):

    ind = 0
    ind += settings["n_coeff"]
    ind += settings["n_img"]
    ind += settings["n_img"]
    if settings["fitSNoffset"]:
        ind += 1
        ind += 1

    SNCmat = Cmat[ind:ind+settings["n_epoch"], ind:ind+settings["n_epoch"]]
    ind += settings["n_epoch"]

    return SNCmat


def parseP(P, settings):
    """P is a vector of parameters."""

    parsed = {}

    ind = 0
    parsed["coeffs"] = reshape_coeffs(P[ind:ind+settings["n_coeff"]],
        settings["splineradius"])
    ind += settings["n_coeff"]

    parsed["dRA"] = P[ind:ind+settings["n_img"]]
    ind += settings["n_img"]
    parsed["dDec"] = P[ind:ind+settings["n_img"]]
    ind += settings["n_img"]

    parsed["sndRA_offset"] = P[ind]
    ind += 1
    parsed["sndDec_offset"] = P[ind]
    ind += 1

    parsed["SN_ampl"] = P[ind:ind+settings["n_epoch"]]
    ind += settings["n_epoch"]

    parsed["pt_RA"] = settings["RA0"]+parsed["dRA"]+parsed["sndRA_offset"]
    parsed["pt_Dec"] = settings["Dec0"]+parsed["dDec"]+parsed["sndDec_offset"]

    return parsed

def unparseP(parsed, settings):
    """parsed is a dictionary of parameters."""

    P = concatenate((unreshape_coeffs(parsed["coeffs"],
        settings["splineradius"]), parsed["dRA"], parsed["dDec"],
        [parsed["sndRA_offset"]], [parsed["sndDec_offset"]], parsed["SN_ampl"]
    ))
    return P


################################################################################

class forward_model():

    def __init__(self, settings_file):

        message("Initializing forward model")
        print(f"version: {version}")

        self.version = version

        self.settings = self.read_paramfile(settings_file)
        self.settings = self.finish_settings(self.settings)

        self.basedir = self.settings["base_dir"]

        message("Getting all data for images")

        self.all_data = dict(scidata=[],
                             invvars=[],
                             RAs=[],
                             Decs=[],
                             psf_FFTs={},
                             psf_subpixelized={})

        self.all_data = self.get_PSFs(self.settings, self.all_data)
        self.all_data = self.get_data(self.settings, self.all_data)

        self.settings["flux_scale"] = scoreatpercentile(
            self.all_data["scidata"], 99)

        self.parsed = {}
        self.parsed["coeffs"] = reshape_coeffs(ones(self.settings["n_coeff"])*\
            self.settings["flux_scale"], radius = self.settings["splineradius"])
        self.parsed["dRA"] = zeros(self.settings["n_img"], dtype=float64)
        self.parsed["dDec"] = zeros(self.settings["n_img"], dtype=float64)
        self.parsed["sndRA_offset"] = self.settings["sndRA_offset"]
        self.parsed["sndDec_offset"] = self.settings["sndDec_offset"]
        self.parsed["SN_ampl"] = ones(self.settings["n_epoch"],
            dtype=float64)*self.settings["flux_scale"]
        self.parsed = parseP(unparseP(self.parsed, self.settings),
            self.settings)

        print("Saving input images:")
        self.save_imgs(self.all_data, self.basedir)


    def save_imgs(self, all_data, basedir):

        for imgtype in ['invvars','scidata','pixel_area_map',
            'pixel_sampled_RAs', 'pixel_sampled_Decs']:
            if imgtype in all_data.keys():
                imgname = os.path.join(basedir, imgtype+'.fits')
                print(f'Saving: {imgname}')
                save_img(all_data[imgtype], imgname)

    def read_paramfile(self, parfl):
        """Read in the settings."""

        f = open(parfl)
        lines = f.read().split('\n')
        f.close()

        settings = {}

        for line in lines:
            parsed = parse_line(line)
            if parsed != None:
                settings[parsed[0]] = parsed[1]

        return settings


    def finish_settings(self, settings):
        """Finalize and check settings."""

        assert settings["patch"] % 2 == 1, "patch must be odd!"
        settings["n_img"] = len(settings["images"])

        settings["fitSNoffset"] # Making sure it's there

        settings["epoch_names"] = [0]

        epoch_numbers = []
        for ep in settings["epochs"]:
            if settings["epoch_names"].count(ep) == 0:
                settings["epoch_names"].append(ep)
            epoch_numbers.append(settings["epoch_names"].index(ep))
        settings["epochs"] = array(epoch_numbers)

        settings["n_epoch"] = max(settings["epochs"])
        settings["oversample2"] = int(floor(settings["oversample"]/2.))
        settings["n_coeff"] = reshape_coeffs(coeffs = zeros(100000),
            radius = settings["splineradius"], return_coeffs_not_size = 0)

        for img in settings["images"]:
            assert img.count("_pam_") == 0
            assert img.count("_pam.") == 0

        assert settings["renormpsf"] == 0

        try:
            settings["iterative_centroid"]
        except:
            settings["iterative_centroid"] = 0

        if settings["iterative_centroid"] and settings["fitSNoffset"]:
            assert 0, "Can't iterate a SN centroid. All images must be fit!"

        for key in ["sciext", "errext", "dqext", "errscale", "pixel_area_map",
            "bad_pixel_list", "RA0", "Dec0", "psfs"]:
            if type(settings[key]) != list:
                settings[key] = [settings[key]]*settings["n_img"]

        if len(settings["psfs"]) == 1:
            settings["psfs"] = [settings["psfs"][0]
                for i in range(settings["n_img"])]

        for key in ["RA0", "Dec0"]:
            settings[key] = array(settings[key])

        for key in ["sciext", "errext", "dqext", "errscale", "pixel_area_map",
            "bad_pixel_list", "images", "epochs", "psfs"]:
            msg=key + " has wrong length!"
            assert len(settings[key]) == settings["n_img"], msg

        return settings


    def read_image(self, im, pam, bad_pix_list, exts, settings, RA0, Dec0):
        """Reads patches from image files."""

        badx = [] ; bady = []

        patch2 = int(floor(settings["patch"]/2.))

        data = []
        f = fits.open(im)

        try:
            mjd = 0.5*(f[0].header["EXPSTART"] + f[0].header["EXPEND"])
        except:

            try:
                mjd = f[0].header["BMJD_OBS"]
            except:
                print("Couldn't read EXPSTART/EXPEND/BMJD_OBS!")
                mjd = 0.


        w = wcs.WCS(f[exts[0]].header)
        pix_xy = w.all_world2pix([[RA0, Dec0]], 1)[0]
        pix_xy = array(around(pix_xy), dtype=int32)

        subxs = arange(settings["padsize"],
            dtype=float64)/settings["oversample"]
        subys = arange(settings["padsize"],
            dtype=float64)/settings["oversample"]

        subxs -= median(arange(settings["patch"]*settings["oversample"],
            dtype=float64)/settings["oversample"])
        subys -= median(arange(settings["patch"]*settings["oversample"],
            dtype=float64)/settings["oversample"])

        subxs += pix_xy[0]
        subys += pix_xy[1]

        subxs, subys = meshgrid(subxs, subys)

        RAs, Decs =  w.all_pix2world(subxs, subys, 1)

        dec_range = Decs.max() - Decs.min()
        pscale = settings["splinepixelscale"]
        rad = settings["splineradius"]
        max_range = pscale*(2*rad + 1)*1.05

        assert dec_range > max_range, "Spline overfills patch!"

        subxs = arange(settings["patch"], dtype=float64)
        subys = arange(settings["patch"], dtype=float64)
        subxs -= median(subxs)
        subys -= median(subys)
        subxs += pix_xy[0]
        subys += pix_xy[1]
        subxs, subys = meshgrid(subxs, subys)
        pixel_sampled_RAs, pixel_sampled_Decs=w.all_pix2world(subxs, subys, 1)

        pixel_sampled_js, pixel_sampled_is = meshgrid(arange(settings["patch"],
            dtype=float64), arange(settings["patch"], dtype=float64))

        patchsq=settings["patch"]**2
        x=reshape(pixel_sampled_RAs,  patchsq)
        y=reshape(pixel_sampled_Decs, patchsq)
        pis=reshape(pixel_sampled_is, patchsq)
        pjs=reshape(pixel_sampled_js, patchsq)
        RADec_to_i = SmoothBivariateSpline(x=x, y=y, z=pis, kx = 1, ky = 1)
        RADec_to_j = SmoothBivariateSpline(x=x, y=y, z=pjs, kx = 1, ky = 1)

        tmp_bad_pix = zeros(f[exts[0]].data.shape, dtype=int32)
        for k in range(len(badx)):
            tmp_bad_pix[bady[k] - 1, badx[k] - 1] = 1

        pixelrange = [pix_xy[1] - patch2 - 1,
                      pix_xy[1] + patch2,
                      pix_xy[0] - patch2 - 1,
                      pix_xy[0] + patch2]

        tmp_bad_pix = robust_index(tmp_bad_pix, pixelrange[0], pixelrange[1],
            pixelrange[2], pixelrange[3])

        print(im, pixelrange[0], pixelrange[1], pixelrange[2], pixelrange[3])

        for ext in exts:
            data.append(robust_index(array(f[ext].data, dtype=float64),
                pixelrange[0], pixelrange[1], pixelrange[2], pixelrange[3]))


            for i in range(settings["patch"]):
                for j in range(settings["patch"]):
                    radius = sqrt((i - patch2)**2 + (j - patch2)**2)
                    if radius**2. >= (patch2 + 0.5)**2:
                        data[-1][i,j] = 0
                    else:
                        if ext == exts[1]:
                            if settings["apodize"]:
                                data[-1][i,j] /= 1 - (radius/(patch2 + 0.5))**8.


        f.close()


        f = fits.open(pam)
        pixel_area_map = robust_index(array(f[1].data, dtype=float64),
                                      pixelrange[0],
                                      pixelrange[1],
                                      pixelrange[2],
                                      pixelrange[3],
                                      fill_value = 1.)
        f.close()

        return data, RAs, Decs, RADec_to_i, RADec_to_j, pixel_area_map, \
            tmp_bad_pix, mjd, pixel_sampled_RAs, pixel_sampled_Decs, pixelrange


    def get_data(self, settings, all_data):
        """Read in the data."""

        all_data["RADec_to_i"] = []
        all_data["RADec_to_j"] = []
        all_data["pixel_area_map"] = []
        all_data["mjd"] = []
        all_data["pixel_sampled_RAs"] = []
        all_data["pixel_sampled_Decs"] = []
        all_data["pixelranges"] = []

        n_img=settings["n_img"]
        message("Ingesting all images")
        for i in range(n_img):

            im=settings["images"][i]
            pam=settings["pixel_area_map"][i]
            bad_pix_list=settings["bad_pixel_list"][i]
            exts=[settings[key][i] for key in ["sciext", "errext", "dqext"]]
            RA0=settings["RA0"][i]
            Dec0=settings["Dec0"][i]

            print(f"Image {i+1} {im}")

            data, RAs, Decs, RADec_to_i, RADec_to_j, \
                pixel_area_map, tmp_bad_pix, mjd, \
                pixel_sampled_RAs, pixel_sampled_Decs, \
                pixelrange = self.read_image(im=im,
                                             pam=pam,
                                             bad_pix_list=bad_pix_list,
                                             exts=exts,
                                             settings=settings,
                                             RA0=RA0,
                                             Dec0=Dec0)

            all_data["RADec_to_i"].append(RADec_to_i)
            all_data["RADec_to_j"].append(RADec_to_j)
            all_data["pixel_area_map"].append(pixel_area_map)
            all_data["mjd"].append(mjd)
            all_data["pixel_sampled_RAs"].append(pixel_sampled_RAs)
            all_data["pixel_sampled_Decs"].append(pixel_sampled_Decs)
            all_data["pixelranges"].append(pixelrange)

            DQ = array(data[2], int32)

            for okaydq in settings["okaydqs"]:
                """E.g., 111 with okaydq = 2: ~ okaydq = 101, so DQ -> 101.
                101 with okaydq = 2: ~ okaydq = 101, so DQ -> 101"""
                DQ = bitwise_and(DQ, ~ okaydq)
            DQ += tmp_bad_pix

            invvars = (data[1]*settings["errscale"][i])**-2. * (DQ == 0)

            bad_inds = where(isinf(invvars) + isnan(invvars) + isnan(data[0]))
            invvars[bad_inds] = 0.
            data[0][bad_inds] = 0.
            assert all(invvars >= 0)

            all_data["invvars"].append(invvars)

            all_data["RAs"].append(RAs)
            all_data["Decs"].append(Decs)
            all_data["scidata"].append(data[0])

        print("\n\n")

        all_data["mjd"] = array(all_data["mjd"])

        return all_data


    def get_PSFs(self, settings, all_data):
        """Read in the PSFs. Store FFTs. In the future, don't bother storing
           FFTs for point sources!!!"""

        all_data["psf_FFTs"] = {}
        all_data["psf_subpixelized"] = {}

        basedir = settings["base_dir"]

        print('PSF parameters:')

        for psf in unique(settings["psfs"]):
            f = fits.open(psf)
            psfdata = array(f[0].data, dtype=float64)
            f.close()

            msg="PSF has multiple pixels at the same maximum!"
            assert sum(psfdata == psfdata.max()) == 1, msg

            print(f"psf shape {psfdata.shape}")
            model_shape = settings["patch"]*settings["oversample"]
            print(f"model shape {model_shape}")
            padsize = int(2**ceil(log2(max(max(psfdata.shape), model_shape))))
            print(f"padsize {padsize}")
            settings["padsize"] = padsize

            psfdata_pad = zeros([padsize]*2, dtype=float64)
            psfdata_pad[:psfdata.shape[0], :psfdata.shape[1]] = psfdata

            max_val = max(psfdata.shape) + (max(psfdata.shape) % 2 == 0)
            psfdata_odd = zeros([max_val]*2, dtype=float64)
            padsize_odd = len(psfdata_odd)
            print(f"padsize_odd {padsize_odd}")
            assert padsize_odd % 2 == 1

            psfdata_odd[:psfdata.shape[0], :psfdata.shape[1]] = psfdata

            if not settings["psf_has_pix"]:
                # Add the pixel convolution
                print("Adding pixel convolution!")

                pixel = zeros([padsize]*2, dtype=float64)
                pixel[:settings["oversample"], :settings["oversample"]] = 1.

                psf_fft = ft.fft2(psfdata_pad) * ft.fft2(pixel)
                psfdata_pad = array(real(ft.ifft2(psf_fft)), dtype=float64)

                pixel = zeros([padsize_odd]*2, dtype=float64)
                pixel[:settings["oversample"], :settings["oversample"]] = 1.

                psf_fft = ft.fft2(psfdata_odd) * ft.fft2(pixel)
                psfdata_odd = array(real(ft.ifft2(psf_fft)), dtype=float64)

            # Now, recenter
            maxinds = where(psfdata_pad == psfdata_pad.max())

            recenter = zeros([padsize]*2, dtype=float64)
            recenter[padsize - maxinds[0][0], padsize - maxinds[1][0]] = 1.

            psfdata_fft = ft.fft2(psfdata_pad) * ft.fft2(recenter)

            psf_test = ft.ifft2(psfdata_fft)
            assert abs(imag(psf_test)).max() < 1e-8

            psf_test = array(real(psf_test), dtype=float64)
            assert psf_test[0,0] == psf_test.max()

            all_data["psf_FFTs"][psf] = psfdata_fft

            maxinds = where(psfdata_odd == psfdata_odd.max())
            print(f"maxinds {maxinds[0][0]} {maxinds[1][0]}")
            recenter = zeros([padsize_odd]*2, dtype=float64)
            recenter[int(floor(psfdata.shape[0]/2.)) - maxinds[0][0],
                int(floor(psfdata.shape[1]/2.)) - maxinds[1][0]] = 1.

            psf_fft = ft.fft2(psfdata_odd) * ft.fft2(recenter)
            psfdata = array(real(ft.ifft2(psf_fft)), dtype=float64)

            all_data["psf_subpixelized"][psf] = psfdata

            save_img(all_data["psf_subpixelized"][psf],
                os.path.join(basedir, "psf_subpixelized.fits"))

            print("\n\n")

        return all_data

    def make_pixelized_PSF(self, parsed, i):

        icoord = self.all_data["RADec_to_i"][i](self.parsed["pt_RA"][i],
            self.parsed["pt_Dec"][i])[0,0]
        jcoord = self.all_data["RADec_to_j"][i](self.parsed["pt_RA"][i],
            self.parsed["pt_Dec"][i])[0,0]

        j2d, i2d = meshgrid(arange(self.settings["patch"],
                                dtype=float64)*self.settings["oversample"],
                            arange(self.settings["patch"],
                                dtype=float64)*self.settings["oversample"])

        psf_subpix = self.all_data["psf_subpixelized"][self.settings["psfs"][i]]
        psfsize = len(psf_subpix)

        i2d -= icoord*self.settings["oversample"] - floor(psfsize/2.)
        j2d -= jcoord*self.settings["oversample"] - floor(psfsize/2.)


        i1d = reshape(i2d, self.settings["patch"]**2)
        j1d = reshape(j2d, self.settings["patch"]**2)
        coords = array([i1d, j1d])

        patch=self.settings["patch"]
        psf_subpix = self.all_data["psf_subpixelized"][self.settings["psfs"][i]]
        pixelized_psf = map_coordinates(psf_subpix,
                                        coordinates=coords,
                                        order=2,
                                        mode="constant",
                                        cval=0,
                                        prefilter=True)
        pixelized_psf = reshape(pixelized_psf, [patch, patch])

        return pixelized_psf

    def indiv_model(self, args):
        [i, parsed, just_pt_flux] = args

        if just_pt_flux:
            pixelized_psf = self.make_pixelized_PSF(parsed, i)
            if self.settings["epochs"][i] > 0:
                pam=self.all_data["pixel_area_map"][i]
                SN_ampl=parsed["SN_ampl"][self.settings["epochs"][i] - 1]
                convolved_model = (pixelized_psf/pam)*(SN_ampl)
            else:
                convolved_model = pixelized_psf*0.
            return convolved_model

        # map_coordinates numbers starting from 0,
        # e.g., radius=3 => 0,1,2,(3),4,5,6
        dec_scale = cos(self.settings["Dec0"][i]/(180./pi))
        pscale = self.settings["splinepixelscale"]
        rad = self.settings["splineradius"]

        pra = parsed["dRA"][i] ; pdec = parsed["dDec"][i]

        ras = self.all_data["RAs"][i]-self.settings["RA0"][i]-pra
        des = self.all_data["Decs"][i]-self.settings["Dec0"][i]-pdec

        xs = ras * dec_scale / pscale + rad
        ys = des / pscale + rad

        xs1D = reshape(xs, self.settings["padsize"]**2)
        ys1D = reshape(ys, self.settings["padsize"]**2)

        coords = array([xs1D, ys1D])

        subsampled_model = map_coordinates(parsed["coeffs"],
                                           coordinates=coords,
                                           order=2,
                                           mode="constant",
                                           cval=0,
                                           prefilter=True)
        subsampled_model = reshape(subsampled_model,
            [self.settings["padsize"], self.settings["padsize"]])

        psf_fft = self.all_data["psf_FFTs"][self.settings["psfs"][i]]
        scm = ft.ifft2(ft.fft2(subsampled_model) * psf_fft)
        scm = array(real(scm), dtype=float64)

        o1=self.settings["oversample"]
        o2=self.settings["oversample2"]
        convolved_model = scm[o2::o1, o2::o1]
        convolved_model = convolved_model[:self.settings["patch"],
            :self.settings["patch"]]

        if self.settings["epochs"][i] > 0:
            pixelized_psf = self.make_pixelized_PSF(parsed, i)
            pam = self.all_data["pixel_area_map"][i]
            SN_ampl = parsed["SN_ampl"][self.settings["epochs"][i] - 1]
            convolved_model += (pixelized_psf/pam)*(SN_ampl)


        if any(self.all_data["invvars"][i] != 0):
            mod_resid = self.all_data["scidata"][i] - convolved_model
            invvar = self.all_data["invvars"][i]
            sky_estimate = sum(mod_resid*invvar)/sum(invvar)
        else:
            sky_estimate = 0.

        convolved_model += sky_estimate

        return convolved_model


    def modelfn(self, parsed, im_ind = None, just_pt_flux = 0):
        """Construct the model."""

        if im_ind == None:
            im_ind = list(range(self.settings["n_img"]))

        pool = multiprocessing.Pool(processes = self.settings["n_cpu"])

        models = pool.map(self.indiv_model,
            [(i, parsed, just_pt_flux) for i in im_ind])

        pool.close()

        return array(models)

    def default_im_ind(self, im_ind):
        if im_ind == None:
            im_ind = list(range(self.settings["n_img"]))
        if type(im_ind) == int:
            im_ind = [im_ind]
        return im_ind

    def pull_FN(self, parsed, im_ind = None):
        im_ind = self.default_im_ind(im_ind)

        models = self.modelfn(parsed, im_ind = im_ind)

        pulls = []
        for i, im in enumerate(im_ind):
            pulls.append((self.all_data["scidata"][im] - models[i])*\
                sqrt(self.all_data["invvars"][im]))

        pulls = array(pulls)
        pulls = reshape(pulls, self.settings["patch"]**2 * len(im_ind))

        dec_scale = cos(self.settings["Dec0"]/57.2957795)*3600.
        dRA_arcsec = (parsed["pt_RA"] - self.settings["RA0"])*dec_scale
        dDec_arcsec = (parsed["pt_Dec"] - self.settings["Dec0"])*3600.

        pulls = concatenate((pulls,
            dRA_arcsec/self.settings["SN_centroid_prior_arcsec"],
            dDec_arcsec/self.settings["SN_centroid_prior_arcsec"]))

        return pulls


    def pull_FN_wrapper(self, P, im_ind_wrap, makechi2 = 0):
        """For L-M"""
        im_ind = im_ind_wrap[0]
        assert type(im_ind) == list

        parsed = parseP(P, self.settings)
        pulls = self.pull_FN(parsed, im_ind)

        if makechi2:
            return dot(pulls, pulls)
        else:
            return pulls

    def LM_fit_for_centroids(self, parsed, offset_scale=1.0e-1):
        P = unparseP(parsed, self.settings)

        pulls = self.pull_FN(parsed)
        assert 1 - isnan(dot(pulls, pulls))

        # Get miniscale parameters
        coeffs = reshape_coeffs(ones(self.settings["n_coeff"]),
            radius=self.settings["splineradius"])
        SN_ampl = ones(self.settings["n_epoch"],
            dtype=float64)*self.settings["flux_scale"]
        dRA = zeros(self.settings["n_img"], dtype=float64)
        dDec = zeros(self.settings["n_img"], dtype=float64)


        miniscale_parsed = dict(coeffs=coeffs,
                                SN_ampl=SN_ampl,
                                sndRA_offset=0,
                                sndDec_offset=0,
                                dRA=dRA,
                                dDec=dDec)
        miniscale = unparseP(miniscale_parsed, self.settings)

        timenow=time.asctime()
        start = time.time()
        print(f"Running galaxy+SN-only fit {timenow}")
        P, F, NA = miniLM_new(ministart=P,
                              miniscale=miniscale,
                              residfn=self.pull_FN_wrapper,
                              passdata=list(range(self.settings["n_img"])),
                              verbose=False,
                              maxiter=1,
                              use_dense_J=True)

        timenow=time.asctime()
        elapsed = time.time() - start
        print(f"Done at {timenow}, {elapsed} total seconds")

        F_fmt = '%11.2f'%F
        F_fmt = F_fmt.strip()
        print(f"LM chi^2 {F_fmt}")

        if self.settings["iterative_centroid"]:
            assert self.settings["fitSNoffset"] == 0

            n_img=self.settings["n_img"]
            for i in range(n_img):
                n_img=self.settings["n_img"]
                print(f"Centroiding {i+1} of {n_img}")
                tmp_dpos = zeros(n_img, dtype=float64)
                tmp_dpos[i] = 0.1

                # Get ith img parameters
                coeffs = reshape_coeffs(zeros(self.settings["n_coeff"]),
                    radius=self.settings["splineradius"])
                SN_ampl = zeros(self.settings["n_epoch"], dtype=float64)

                miniscale_parsed = dict(coeffs=coeffs,
                                        SN_ampl=SN_ampl,
                                        sndRA_offset=0,
                                        sndDec_offset=0,
                                        dRA=tmp_dpos,
                                        dDec=tmp_dpos)
                miniscale = unparseP(miniscale_parsed, self.settings)
                P, F, Cmat = miniLM_new(ministart=P,
                                        miniscale=miniscale,
                                        residfn=self.pull_FN_wrapper,
                                        passdata=[i],
                                        verbose=False,
                                        maxiter=3)
        else:

            timenow=time.asctime()
            print(f"Running centroid-only fit {timenow}")

            coeffs = reshape_coeffs(zeros(self.settings["n_coeff"]),
                radius=self.settings["splineradius"])
            SN_ampl = zeros(self.settings["n_epoch"], dtype=float64)
            sndRA_offset = offset_scale * self.settings["fitSNoffset"]
            sndDec_offset = offset_scale * self.settings["fitSNoffset"]
            dRA = zeros(n_img, dtype=float64) + offset_scale
            dDec = zeros(n_img, dtype=float64) + offset_scale

            miniscale_parsed = dict(coeffs=coeffs,
                                    SN_ampl=SN_ampl,
                                    sndRA_offset=sndRA_offset,
                                    sndDec_offset=sndDec_offset,
                                    dRA=dRA,
                                    dDec=dDec)
            miniscale = unparseP(miniscale_parsed, self.settings)
            P, F, NA = miniLM_new(ministart=P,
                                  miniscale=miniscale,
                                  residfn=self.pull_FN_wrapper,
                                  passdata=list(range(n_img)),
                                  verbose=False,
                                  maxiter=3,
                                  use_dense_J=True)

            print(f"LM chi^2 {F}")
            timenow=time.asctime()
            print(f"Running everything fit {timenow}")

            coeffs = reshape_coeffs(ones(self.settings["n_coeff"])*\
                self.settings["flux_scale"],
                radius=self.settings["splineradius"])
            SN_ampl = ones(self.settings["n_epoch"],
                dtype=float64)*self.settings["flux_scale"]
            sndRA_offset = offset_scale * self.settings["fitSNoffset"]
            sndDec_offset = offset_scale * self.settings["fitSNoffset"]
            dRA = zeros(n_img, dtype=float64) + offset_scale
            dDec = zeros(n_img, dtype=float64) + offset_scale

            miniscale_parsed = dict(coeffs=coeffs,
                                    SN_ampl=SN_ampl,
                                    sndRA_offset=sndRA_offset,
                                    sndDec_offset=sndDec_offset,
                                    dRA=dRA,
                                    dDec=dDec)
            miniscale = unparseP(miniscale_parsed, self.settings)
            P, F, Cmat = miniLM_new(ministart=P,
                                    miniscale=miniscale,
                                    residfn=self.pull_FN_wrapper,
                                    passdata=list(range(n_img)),
                                    verbose=False,
                                    maxiter=3,
                                    use_dense_J=True)

            try:
                Cmat[0,0]
            except:
                Cmat = zeros([sum(miniscale != 0)]*2, dtype=float64)

            print(f"LM chi^2 {F}")

        parsed = parseP(P, self.settings)

        return parsed, Cmat

    def time_to_stop(self, parsed, last_flux, chi2, last_chi2, settings, itr):
        if itr >= settings["n_iter"]:
            print("Reached maximum iterations!")
            return 1
        if len(parsed["SN_ampl"]) == 0:
            if last_chi2 - chi2 < 0.01:
                print("Chi^2 converged for galaxy-only run!")
                return 1
        else:
            new_SN_ampl_with_floor = abs(parsed["SN_ampl"])+abs(last_flux.max())
            old_SN_ampl_with_floor = abs(last_flux)+abs(last_flux.max())

            ampl_diff = abs(new_SN_ampl_with_floor/old_SN_ampl_with_floor).max()
            print(f"ampl_diff {ampl_diff}")

            if abs(ampl_diff - 1.) < 0.0001:
                return 1

        return 0

    def do_main_reduction(self, parsed, settings):

        last_flux = zeros(len(parsed["SN_ampl"]), dtype=float64) - 2.
        last_chi2 = 1e101
        itr = 0
        chi2 = 1e100
        Cmat = None
        stop = 0
        max_iter = settings["n_iter"]
        n_img = settings["n_img"]

        while stop==0:

            last_chi2 = chi2
            last_flux = parsed["SN_ampl"]

            message(f"Running LM fit for iteration {itr+1} of {max_iter}")

            parsed, Cmat = self.LM_fit_for_centroids(parsed)

            pulls = self.pull_FN(parsed)
            pRA = parsed["dRA"]
            dDec = parsed["dDec"]
            chi2 = dot(pulls, pulls)
            chi2_fmt = '%11.2f'%chi2
            chi2_fmt = chi2_fmt.strip()

            print(f"chi^2 check for iter {itr+1} after centroid {chi2_fmt}")

            itr += 1

            stop = self.time_to_stop(parsed, last_flux, chi2, last_chi2,
                settings, itr)

        assert itr > 0, "No iterations run!"

        # Parse models, residuals, pulls, and pt_models and save
        models = self.modelfn(parsed)
        save_img(models, os.path.join(self.basedir, "models.fits"))

        residuals = [(self.all_data["scidata"][i] - models[i])*\
            (self.all_data["invvars"][i] > 0) for i in range(n_img)]
        save_img(residuals, os.path.join(self.basedir, "residuals.fits"))

        pulls = array([(self.all_data["scidata"][i] - models[i])*\
            sqrt(self.all_data["invvars"][i]) for i in range(n_img)])
        save_img(pulls, os.path.join(self.basedir, "pulls.fits"))

        pt_models = self.modelfn(parsed, just_pt_flux = 1)
        save_img(pt_models, os.path.join(self.basedir, "pt_models.fits"))

        try:
            SNCmat = parseCmat(Cmat, settings)
            print('Successfully parsed SNCmat')
        except:
            SNCmat = zeros([len(parsed["SN_ampl"])]*2)
            print('Populating SNCmat with zeros')

        self.create_fit_results(self.all_data, parsed,
            settings, SNCmat, Cmat, chi2, pulls)

    def create_fit_results(self, all_data, parsed, settings, SNCmat, Cmat, chi2,
        pulls, pklbasename='fit_results.pickle', resultsbasename='results.txt'):

        print(f'Dumping data into {pklbasename}')
        pickle.dump([all_data, parsed, settings, SNCmat, Cmat],
            gzip.open(os.path.join(self.basedir, pklbasename), 'w'))

        with open(os.path.join(self.basedir, resultsbasename), 'w') as f:

            f.write(f"version {self.version} \n")
            f.write(f"chi2 {chi2} \n")

            n_pixels = (array(all_data["invvars"]) > 0).sum()
            f.write(f"Npixels  {n_pixels} \n")

            dof = n_pixels - len(Cmat)
            f.write(f"DoF {dof} \n")

            for pull_thresh in [5, 10, 20, 50]:
                npix_pull_gt = sum(abs(pulls) > pull_thresh)
                f.write(f"Npixels_with_pull_gt_{pull_thresh} {npix_pull_gt} \n")

            f.write('\n')
            f.write('SNCmat:\n')

            for i in range(settings["n_epoch"]):
                try:
                    ename = settings["epoch_names"][i+1]
                    SN_ampl = parsed["SN_ampl"][i]
                    SN_data = sqrt(SNCmat[i,i])
                    f.write(f"SN_A{ename} {SN_ampl} {SN_data} \n")
                except IndexError:
                    print(f'ERROR: SNCmat does not have supernova data')
                    continue

            f.write('\n')
            f.write('Mean MJD:\n')

            for i in range(settings["n_epoch"]):
                ename = settings["epoch_names"][i+1]
                inds = where(settings["epochs"] == i+1)
                mean_data = mean(array(all_data["mjd"])[inds])

                f.write(f"MJD_{ename} {mean_data} \n")

            f.write('\n')
            f.write('Cmat:\n')

            for i in range(settings["n_epoch"]):
                for j in range(settings["n_epoch"]):
                    try:
                        SN_data = SNCmat[i,j]
                        f.write(f"{i},{j}: {SN_data} \n")
                    except IndexError:
                        print(f'ERROR: SNCmat does not have supernova data')
                        continue
                f.write('\n')

            try:
                SNWmat = linalg.inv(SNCmat)
            except:
                SNWmat = SNCmat*0

            f.write('\n')
            f.write('Wmat:\n')

            for i in range(settings["n_epoch"]):
                for j in range(settings["n_epoch"]):
                    try:
                        SN_data = SNWmat[i,j]
                        f.write(f"{i},{j}: {SN_data} \n")
                    except IndexError:
                        print(f'ERROR: SNWmat does not have supernova data')
                        continue
                f.write('\n')

            f.write("PARSED_JSON_BELOW\n")

            for key in parsed:
                try:
                    parsed[key] = parsed[key].tolist()
                except:
                    pass

            f.write(json.dumps(parsed) + '\n')

if __name__ == "__main__":

    fm = forward_model(sys.argv[1])
    fm.do_main_reduction(fm.parsed, fm.settings)

    print("Done!")
