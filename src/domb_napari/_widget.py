from magicgui import magic_factory

from napari import Viewer
from napari.layers import Image, Labels
from napari.utils.notifications import show_info
from napari.qt.threading import thread_worker

import pathlib
import os

import numpy as np
from numpy import ma
from scipy import ndimage as ndi
from scipy import stats
from scipy import signal

from skimage import filters
from skimage import morphology
from skimage import measure
from skimage import restoration
from skimage import feature
from skimage import segmentation

import vispy.color

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvas

from dipy.align.transforms import AffineTransform2D
from dipy.align.imaffine import AffineRegistration

from domb.utils import masking
from domb.fret.e_fret import e_app


@magic_factory(call_button='Preprocess stack',
               stack_order={"choices": ['TCXY', 'CTXY']},
               correction_method={"choices": ['exp', 'bi_exp']},)
def split_channels(viewer: Viewer, img:Image,
                   stack_order:str='TCXY',
                   median_filter:bool=True, median_kernel:int=2,  #gaussian_blur:bool=True, gaussian_sigma=0.75,
                   background_substraction:bool=True,
                   photobleaching_correction:bool=False,
                   correction_method:str='exp',
                   drop_frames:bool=False,
                   frames_range:list=[0,10]):
    if input is not None:
        def _save_ch(params):
            img = params[0]
            img_name = params[1]
            try: 
                viewer.layers[img_name].data = img
            except KeyError:
                new_image = viewer.add_image(img, name=img_name, colormap='turbo')

        @thread_worker(connect={'yielded':_save_ch})
        def _split_channels():
            def _preprocessing(ch_img, ch_suffix):
                if drop_frames:
                    if len(frames_range) == 2:
                        ch_img = ch_img[frames_range[0]:frames_range[-1],:,:]
                        ch_suffix = f'_{frames_range[0]}-{frames_range[-1]}'+ch_suffix
                    else:
                        raise ValueError('List of indexes should has 2 elements!')
                if median_filter:
                    median_axis = lambda x,k: np.array([ndi.median_filter(f, size=k) for f in x], dtype=x.dtype)
                    ch_img = median_axis(ch_img, median_kernel)
                if background_substraction:
                    bc_p = lambda x: np.array([f - np.percentile(f, 0.5) for f in x]).clip(min=0).astype(dtype=x.dtype)
                    ch_img = bc_p(ch_img)
                if photobleaching_correction:
                    pb_mask = masking.proc_mask(np.mean(ch_img, axis=0))
                    ch_img,_,r_corr = masking.pb_exp_correction(input_img=ch_img,
                                                                mask=pb_mask,
                                                                method=correction_method)
                    show_info(f'{correction_method} photobleaching correction, r^2={r_corr}')
                return (ch_img, img.name+ch_suffix)

            if img.data.ndim == 4:
                show_info(f'{img.name}: Ch. split and preprocessing mode, shape {img.data.shape}')
                if stack_order == 'TCXY':
                    input_img = img.data
                elif stack_order == 'CTXY':
                    input_img = np.moveaxis(img.data,0,1)
                for i in range(0,img.data.shape[1]):
                    show_info(f'{img.name}: Ch. {i} preprocessing')
                    yield _preprocessing(ch_img=input_img[:,i,:,:], ch_suffix=f'_ch{i}')
            elif img.data.ndim == 3:
                show_info(f'{img.name}: Image already has 3 dimensions, preprocessing only mode')
                yield _preprocessing(ch_img=img.data, ch_suffix=f'_ch0')
            else:
                raise ValueError('Input image should have 3 or 4 dimensions!')       
        
        _split_channels()


@magic_factory(call_button='Align stack')
def dw_registration(viewer: Viewer, offset_img:Image, reference_img:Image,
                    use_reference_img:bool=False, input_crop:int=25, output_crop:int=20):
    if input is not None:
        if offset_img.data.ndim == 4:

            def _save_aligned(img):
                xform_name = offset_img.name+'_xform'
                try: 
                    viewer.layers[xform_name].data = img
                    viewer.layers[xform_name].colormap = 'turbo'
                except KeyError:
                    viewer.add_image(img, name=xform_name, colormap='turbo')

            @thread_worker(connect={'yielded':_save_aligned})
            def _dw_registration():
                offset_series = offset_img.data
                master_img = reference_img.data

                if input_crop != 0:
                    y, x = offset_series.shape[-2:]
                    offset_series = offset_series[:,:,input_crop:y-input_crop,input_crop:x-input_crop]
                    master_img = master_img[:,input_crop:y-input_crop,input_crop:x-input_crop]

                if use_reference_img:
                    master_img_ref, master_img_offset = master_img[1], master_img[0]
                else:
                    master_img_ref = np.mean(offset_series[:,3,:,:], axis=0)
                    master_img_offset = np.mean(offset_series[:,0,:,:], axis=0)

                affreg = AffineRegistration()
                transform = AffineTransform2D()
                affine = affreg.optimize(master_img_ref, master_img_offset,
                                        transform, params0=None)
                master_img_xform = affine.transform(master_img_offset)

                # masking.misalign_estimate(master_img_ref, master_img_offset,
                #                           title='Master raw', show_img=False, rough_estimate=False)
                # masking.misalign_estimate(master_img_ref, master_img_xform,
                #                           title='Master xform', show_img=False, rough_estimate=False)

                ch0_xform = np.asarray([affine.transform(frame) for frame in offset_series[:,0,:,:]])
                ch2_xform = np.asarray([affine.transform(frame) for frame in offset_series[:,2,:,:]])
                xform_series = np.stack((ch0_xform,
                                         offset_series[:,1,:,:],
                                         ch2_xform,
                                         offset_series[:,3,:,:]),
                                        axis=1)
                if output_crop != 0:
                    yo, xo = xform_series.shape[-2:]
                    xform_series = xform_series[:,:,output_crop:yo-output_crop,output_crop:xo-output_crop]
                
                yield xform_series.astype(offset_series.dtype)
                    
            _dw_registration()
        else:
            raise ValueError('Incorrect input image shape!')


@magic_factory(call_button='Split SEP',
               pH_1st_frame={"choices": ['7.3', '6.0']},)
def split_sep(viewer: Viewer, img:Image,
              pH_1st_frame:str='7.3',
              calc_surface_img:bool=False,
              calc_projections:bool=False):
    if input is not None:
        if img.data.ndim == 3:

            def _save_sep(params):
                img = params[0]
                img_name = params[1]
                cmap_rg = False
                if len(params) == 3:
                    cmap_rg = params[2]
                try: 
                    viewer.layers[img_name].data = img
                except KeyError:
                    new_image = viewer.add_image(img, name=img_name, colormap='turbo')
                    if cmap_rg:
                        new_image.colormap = 'red-green', _red_green()
                    else:
                        new_image.colormap = 'turbo'

            @thread_worker(connect={'yielded':_save_sep})
            def _split_sep():
                sep_img = img.data.astype(float)

                if pH_1st_frame == '7.3':
                    total_start_i, intra_start_i = 0, 1
                elif pH_1st_frame == '6.0':
                    total_start_i, intra_start_i = 1, 0

                total_img = sep_img[total_start_i::2,:,:]  # 0
                intra_img = sep_img[intra_start_i::2,:,:]  # 1

                total_name = img.name + '_total'
                intra_name = img.name + '_intra'

                yield (total_img, total_name)
                yield (intra_img, intra_name)

                if calc_projections:
                    projections_diff = lambda x: np.max(x, axis=0) - np.mean(x, axis=0)
                    yield (projections_diff(total_img),
                           img.name + '_total-projection',
                           True)
                    yield (projections_diff(intra_img),
                           img.name + '_intra-projection',
                           True)
                    yield (np.max(intra_img, axis=0),
                           img.name + '_intra-mip')

                if calc_surface_img:
                    surface_img = total_img - intra_img
                    yield (surface_img,
                           img.name + '_surface')
                    if calc_projections:
                        yield (projections_diff(surface_img),
                               img.name + '_surface-projection',
                               True)
            
            _split_sep()
        else:
            raise ValueError('The input image should have 3 dimensions!')


@magic_factory(call_button='Calc E-FRET',
               output_type={"choices": ['Eapp', 'Ecorr', 'Fc']},)
def e_app_calc(viewer: Viewer, DD_img:Image, DA_img:Image, AA_img:Image,
               a:float=0.122, d:float=0.794, G:float=3.6,
               output_type:str='Eapp',
               save_normalized:bool=True):
    if input is not None:
        if (DD_img.data.ndim == 3) and (DA_img.data.ndim == 3) and (AA_img.data.ndim == 3):

            def _save_e_app(params):
                img = params[0]
                img_name = params[1]
                try: 
                    viewer.layers[img_name].data = img
                except KeyError:
                    viewer.add_image(img, name=img_name, colormap='turbo')

            @thread_worker(connect={'yielded':_save_e_app})
            def _e_app_calc():
                e_fret_img = e_app.Eapp(dd_img=DD_img.data, da_img=DA_img.data, aa_img=AA_img.data,
                                        abcd_list=[a,0,0,d], G_val=G,
                                        mask=masking.proc_mask(np.mean(AA_img.data, axis=0)))
                output_name = AA_img.name.replace('_ch3','')
                if output_type == 'Ecorr':
                    output_fret_img = e_fret_img.Ecorr_img
                    output_suffix = '_Ecorr'
                elif output_type == 'Eapp':
                    output_fret_img = e_fret_img.Eapp_img
                    output_suffix = '_Eapp'
                elif output_type == 'Fc':
                    output_fret_img = e_fret_img.Fc_img
                    output_suffix = '_Fc'
                yield (output_fret_img, output_name + output_suffix)
                if save_normalized:
                    img_norm = np.mean(AA_img.data, axis=0)
                    img_norm = (img_norm-np.min(img_norm)) / (np.max(img_norm)-np.min(img_norm))
                    output_norm = output_fret_img*img_norm
                    yield (output_norm, output_name + output_suffix + '_norm')

            _e_app_calc()
        else:
            raise ValueError('Incorrect input image shape!')


@magic_factory(call_button='Calc Red-Green')
def der_series(viewer: Viewer, img:Image,
               left_frames:int=1, space_frames:int=1, right_frames:int=1,
               normalize_by_int:bool=True,
               save_MIP:bool=False):
    if input is not None:
        if img.data.ndim != 3:
            raise ValueError('The input image should have 3 dimensions!')

        def _red_green():
            """ Red-green colormap

            """
            return vispy.color.Colormap([[0.0, 1.0, 0.0],
                                        [0.0, 0.9, 0.0],
                                        [0.0, 0.85, 0.0],
                                        [0.0, 0.0, 0.0],
                                        [0.85, 0.0, 0.0],
                                        [0.9, 0.0, 0.0],
                                        [1.0, 0.0, 0.0]])

        def _save_rg_img(params):
            img = params[0]
            img_name = params[1]
            try: 
                viewer.layers[img_name].data = img
            except KeyError:
                if normalize_by_int:
                    c_lim = np.max(np.abs(img)) * 0.3
                else:
                    c_lim = np.max(np.abs(img)) * 0.75
                new_image = viewer.add_image(img, name=img_name, contrast_limits=[-c_lim, c_lim])
                new_image.colormap = 'red-green', _red_green()

        @thread_worker(connect={'yielded':_save_rg_img})
        def _der_series():
            ref_img = img.data

            der_img = []
            for i in range(ref_img.shape[0]-(left_frames+right_frames+space_frames)):
                img_base = np.mean(ref_img[i:i+left_frames+1], axis=0)
                img_stim = np.mean(ref_img[i+left_frames+right_frames:i+left_frames+right_frames+space_frames+1], axis=0)
                
                img_diff = img_stim-img_base

                if normalize_by_int:
                    img_norm = np.mean(np.stack((img_base,img_diff), axis=0), axis=0)
                    img_norm = (img_norm-np.min(img_norm)) / (np.max(img_norm)-np.min(img_norm))
                    img_diff = img_diff * img_norm

                der_img.append(img_diff)

            der_img = np.asarray(der_img, dtype=float)
            yield (der_img, img.name + '_red-green')

            if save_MIP:
                der_mip = np.max(der_img, axis=0)
                yield (der_mip, img.name + '_red-green-MIP')

        _der_series()


@magic_factory(call_button='Build Dots Mask',
               background_level={"widget_type": "FloatSlider", 'min':50.0, 'max': 99.0, 'step':1.0},
               detection_level={"widget_type": "FloatSlider",'min':1.0, 'max': 100.0, 'step':1.0},)
def dot_mask_calc(viewer: Viewer, img:Image, background_level:float=75.0, detection_level:float=25.0,
                  minimal_distance:int=2, mask_diamets:int=5):
    if input is not None:
        if img.data.ndim != 3:
            raise ValueError('The input image should have 3 dimensions!')
        labels_name = img.name + '_dots-labels'

        def _save_dot_labels(params):
            lab = params[0]
            name = params[1]
            try:
                viewer.layers[name].data = lab
            except KeyError:
                new_labels = viewer.add_labels(lab, name=name, opacity=1)
                new_labels.contour = 1

        @thread_worker(connect={'yielded':_save_dot_labels})
        def _dot_mask_calc():
            prc_filt = lambda x, p: np.array(x - np.percentile(x, p)).clip(min=0).astype(dtype=x.dtype)

            input_img = img.data
            input_mip = np.max(input_img, axis=0)
            detection_mip = prc_filt(x=input_mip, p=background_level)

            peaks_coord = feature.peak_local_max(detection_mip,
                                                 min_distance=2,
                                                 threshold_rel=detection_level/100.0)
            peaks_img = np.zeros_like(input_mip, dtype=bool)
            peaks_img[tuple(peaks_coord.T)] = True
            peaks_mask = morphology.dilation(peaks_img, footprint=morphology.disk(mask_diamets))

            mask_dist_img = ndi.distance_transform_edt(peaks_mask)
            mask_centers_coord = feature.peak_local_max(mask_dist_img,
                                                        min_distance=minimal_distance)
            mask_centers = np.zeros_like(input_mip, dtype=bool)
            mask_centers[tuple(mask_centers_coord.T)] = True

            peaks_labels = segmentation.watershed(-mask_dist_img,
                                                  markers=morphology.label(mask_centers),
                                                  mask=peaks_mask,
                                                  compactness=10)
            show_info(f'{img.name}: detected {np.max(peaks_labels)} dots labels')
            yield (peaks_labels, labels_name)

        _dot_mask_calc()


@magic_factory(call_button='Build Up Mask',
               det_th={"widget_type": "FloatSlider", 'max': 1},
               in_ROIs_det_method={"choices": ['otsu', 'threshold']},)  # insertions_threshold={'widget_type': 'FloatSlider', 'max': 1}
def up_mask_calc(viewer: Viewer, img:Image, ROIs_mask:Labels,
                 det_frame_index:int=2,
                 det_th:float=0.25,
                 in_ROIs_det:bool=True,
                 in_ROIs_det_method:str='otsu',
                 in_ROIs_det_th_corr:float=0.1,
                 final_opening_fp:int=1,
                 final_dilation_fp:int=0,
                 save_total_up_mask:bool=False):
    if input is not None:
        if img.data.ndim != 3:
            raise ValueError('The input image should have 3 dimensions!')

        def _save_up_labels(params):
            lab = params[0]
            name = params[1]
            try:
                viewer.layers[name].data = lab
            except KeyError:
                new_labels = viewer.add_labels(lab, name=name, opacity=1)
                new_labels.contour = 1

        @thread_worker(connect={'yielded':_save_up_labels})
        def _up_mask_calc():
            input_img = img.data
            detection_img = input_img[det_frame_index]

            def up_detection(img, method, th, div, op_f, d_f):
                if method == 'threshold':
                    up_m = img > np.max(np.abs(img)) * (th*div)
                    up_m = morphology.erosion(up_m, footprint=morphology.disk(2))
                    up_m = morphology.dilation(up_m, footprint=morphology.disk(1))
                    up_m = ndi.binary_fill_holes(up_m)
                    up_m = up_m.astype(int)
                elif method == 'otsu':
                    up_m = img > filters.threshold_otsu(img)
                if op_f != 0:
                    up_m = morphology.opening(up_m, footprint=morphology.disk(op_f))
                up_m = morphology.dilation(up_m, footprint=morphology.disk(d_f))
                return up_m.astype(bool)

            if in_ROIs_det:
                rois_mask = ROIs_mask.data
                up_labels = np.zeros_like(rois_mask)
                for roi_region in measure.regionprops(rois_mask):
                    one_roi_box = roi_region.bbox
                    one_roi_img = detection_img[one_roi_box[0]:one_roi_box[2],one_roi_box[1]:one_roi_box[3]]
                    one_roi_input_mask = rois_mask[one_roi_box[0]:one_roi_box[2],one_roi_box[1]:one_roi_box[3]] == 0

                    one_roi_mask = up_detection(img=one_roi_img,
                                                method=in_ROIs_det_method,
                                                th=det_th,
                                                div=in_ROIs_det_th_corr,
                                                op_f=final_opening_fp,
                                                d_f=final_dilation_fp)
                    one_roi_mask[one_roi_input_mask] = 0
                    one_roi_mask = one_roi_mask * roi_region.label
                    up_labels[one_roi_box[0]:one_roi_box[2],one_roi_box[1]:one_roi_box[3]] = one_roi_mask
                    up_mask = up_labels > 0
            else:
                up_mask = up_detection(img=detection_img,
                                       method='threshold',
                                       th=det_th,
                                       div=0.1,
                                       op_f=final_opening_fp,
                                       d_f=final_dilation_fp)
                up_labels = measure.label(up_mask)

            show_info(f'{img.name}: detected {np.max(measure.label(up_mask))} labels')

            labels_name = img.name + '_up-labels'
            yield (up_labels, labels_name)
            if save_total_up_mask:
                mask_name = img.name + '_up-mask'
                yield (up_mask, mask_name)

        _up_mask_calc()                


@magic_factory(call_button='Build Mask',
               masking_mode={"choices": ['up', 'down']},)
def mask_calc(viewer: Viewer, img:Image, det_frame_index:int=2,
              masking_mode:str='up',
              up_threshold:float=0.2,
              down_threshold:float=-0.9,
              opening_footprint:int=0):
    if input is not None:
        if img.data.ndim != 3:
            raise ValueError('The input image should have 3 dimensions!')

        if masking_mode == 'up':
            labels_name = img.name + '_up-labels'
        elif masking_mode == 'down':        
            labels_name = img.name + '_down-labels'

        def _save_rg_labels(params):
            lab = params[0]
            name = params[1]
            try:
                viewer.layers[name].data = lab
            except KeyError:
                new_labels = viewer.add_labels(lab, name=labels_name, opacity=1)
                new_labels.contour = 1

        @thread_worker(connect={'yielded':_save_rg_labels})
        def _mask_calc():
            input_img = img.data
            detection_img = input_img[det_frame_index]

            if masking_mode == 'up':
                mask = detection_img >= np.max(np.abs(detection_img)) * up_threshold
            elif masking_mode == 'down':        
                mask = detection_img <= np.max(np.abs(detection_img)) * down_threshold

            mask = morphology.erosion(mask, footprint=morphology.disk(2))
            mask = morphology.dilation(mask, footprint=morphology.disk(1))
            mask = ndi.binary_fill_holes(mask)
            mask = mask.astype(int)

            if opening_footprint != 0:
                mask = morphology.opening(mask, footprint=morphology.disk(opening_footprint))
                mask = morphology.dilation(mask, footprint=morphology.disk(1))

            labels = measure.label(mask)
            show_info(f'{img.name}: detected {np.max(labels)} "{masking_mode}" labels')

            yield (labels, labels_name)

        _mask_calc()
            

@magic_factory(call_button='Build Profiles',
               saving_path={'mode': 'd'})
def labels_profile_line(viewer: Viewer, img:Image, labels:Labels,
                        time_scale:float=5.0,
                        absolute_intensity:bool=False,
                        ΔF_win:int=5,
                        ΔF_aplitude_lim:list=[10.0, 10.0],
                        profiles_crop:bool=False,
                        profiles_range:list=[0,10],
                        save_data_frame:bool=False,
                        save_ROIs_distances_in_data_frame:bool=False,
                        saving_path:pathlib.Path = os.getcwd()):
    if input is not None:
        input_img = img.data
        input_labels = labels.data
        df_name = img.name + '_' + labels.name
        df_name = df_name.replace('_xform','')
        time_line = np.linspace(0, (input_img.shape[0]-1)*time_scale, \
                                num=input_img.shape[0])

        if save_ROIs_distances_in_data_frame:
            col_list = ['id','roi','int', 'dist', 'index', 'time']
            tip_position_img = np.ones_like(input_img[0], dtype=bool)
            tip_x, tip_y = tip_position_img.shape[0]//2, tip_position_img.shape[1]//2
            tip_position_img[tip_x,tip_y] = False
            tip_distance_img = ndi.distance_transform_edt(tip_position_img)
            distance_list = []
            show_info(f'{img.name}: center position {tip_x, tip_y}')
        else:
            col_list = ['id','roi','int', 'index', 'time']

        profile_raw = []
        profile_dF = []
        for label_num in np.unique(input_labels)[1:]:
            region_mask = input_labels == label_num
            one_prof = np.mean(input_img, axis=(1,2), where=region_mask)
            F_0 = np.mean(one_prof[:ΔF_win])
            one_prof_df = (one_prof-F_0)/F_0

            profile_raw.append(one_prof)
            profile_dF.append(one_prof_df)

            if save_ROIs_distances_in_data_frame:
                distance_list.append(round(np.mean(tip_distance_img, where=region_mask)))

        profile_raw = np.asarray(profile_raw)
        profile_dF = np.asarray(profile_dF)

        profile_to_plot = []
        if absolute_intensity:
            profile_to_plot = np.round(profile_raw)
            ylab = 'Intensity, a.u.'
            df_name = df_name + '_abs'
        else:
            profile_to_plot = np.round(profile_dF, decimals=4)
            ylab = 'ΔF/F0'
            df_name = df_name + '_ΔF'

        if save_data_frame:
            import pandas as pd
            output_df = pd.DataFrame(columns=col_list)

            for num_ROI in range(profile_to_plot.shape[0]):
                profile_ROI = profile_to_plot[num_ROI]

                dict_ROI = {'id':img.name,
                            'roi':num_ROI+1,
                            'int':profile_ROI,
                            'index': np.linspace(0, input_img.shape[0], num=input_img.shape[0], dtype=int),
                            'time':time_line}
                if save_ROIs_distances_in_data_frame:
                    dict_ROI['dist'] = distance_list[num_ROI]

                df_ROI = pd.DataFrame(dict_ROI)
                output_df = pd.concat([output_df.astype(df_ROI.dtypes),
                                       df_ROI.astype(output_df.dtypes)],
                                      ignore_index=True)
            output_df.to_csv(os.path.join(saving_path, df_name+'.csv'))

        # plotting
        if profiles_crop:
            profile_to_plot = profile_to_plot[:,profiles_range[0]:profiles_range[1]]
            time_line = time_line[profiles_range[0]:profiles_range[1]]

        lab_colors = labels.get_color([prop['label'] for prop in measure.regionprops(label_image=input_labels)])

        mpl_fig = plt.figure()
        ax = mpl_fig.add_subplot(111)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        for num_ROI, color in enumerate(lab_colors):
            profile_ROI = profile_to_plot[num_ROI]
            if absolute_intensity:
                ax.plot(time_line, profile_ROI,
                         alpha=0.45, marker='o', color=color)
                plt_title = f'{img.name} absolute intensity profiles, labels {labels.name}'
            elif (profile_ROI.min() > -ΔF_aplitude_lim[0]) | (profile_ROI.max() < ΔF_aplitude_lim[1]):
                ax.plot(time_line, profile_ROI,
                         alpha=0.45, marker='o', color=color)
                plt_title = f'{img.name} ΔF/F0 profiles (lim -{ΔF_aplitude_lim[0]}, {ΔF_aplitude_lim[1]}), labels {labels.name}'
            else:
                continue
        ax.grid(color='grey', linewidth=.25)
        ax.set_xlabel('Time, s')
        ax.set_ylabel(ylab)
        plt.title(plt_title)
        viewer.window.add_dock_widget(FigureCanvas(mpl_fig), name='ROIs Prof.')


@magic_factory(call_button='Build Profiles',
               stat_method={"choices": ['se', 'iqr', 'ci']},
               profiles_num={"choices": ['1', '2', '3']},)
def labels_multi_profile_stat(viewer: Viewer, img_0:Image, img_1:Image, img_2:Image,
                              lab:Labels,
                              profiles_num:str='1',
                              absolute_intensity:bool=False, 
                              time_scale:float=5.0,
                              ΔF_win:int=5,
                              stat_method:str='se'):
    if input is not None:
        # mean, se
        arr_se_stat = lambda x: (np.mean(x, axis=0), \
                                 np.std(x, axis=0)/np.sqrt(x.shape[1]))
        # meadian, IQR
        arr_iqr_stat = lambda x: (np.median(x, axis=0), \
                                  stats.iqr(x, axis=0))
        # mean, CI
        arr_ci_stat = lambda x, alpha=0.05: (np.mean(x, axis=0), \
                                             stats.t.ppf(1-alpha/2, df=x.shape[1]-1) \
                                                         *np.std(x, axis=0, ddof=1)/np.sqrt(x.shape[1]))
        stat_dict = {'se':arr_se_stat,
                     'iqr':arr_iqr_stat,
                     'ci':arr_ci_stat}

        # processing
        input_img_0 = img_0.data
        input_labels = lab.data
        
        profile_dF_0, profile_raw_0 = masking.label_prof_arr(input_label=input_labels,
                                                             input_img_series=input_img_0,
                                                             f0_win=ΔF_win)
        if absolute_intensity:
            selected_profile_0  = profile_raw_0
            ylab = 'I'
        else:
            selected_profile_0  = profile_dF_0
            ylab = 'ΔF/F0'
        arr_val_0, arr_var_0 = stat_dict[stat_method](selected_profile_0)

        if profiles_num == '2' or profiles_num == '3':
            input_img_1 = img_1.data
            profile_dF_1, profile_raw_1 = masking.label_prof_arr(input_label=input_labels,
                                                                 input_img_series=input_img_1,
                                                                 f0_win=ΔF_win)
            if absolute_intensity:
                selected_profile_1  = profile_raw_1
            else:
                selected_profile_1  = profile_dF_1
            arr_val_1, arr_var_1 = stat_dict[stat_method](selected_profile_1)

        if profiles_num == '3':
            input_img_2 = img_2.data
            profile_dF_2, profile_raw_2 = masking.label_prof_arr(input_label=input_labels,
                                                                 input_img_series=input_img_2,
                                                                 f0_win=ΔF_win)
            if absolute_intensity:
                selected_profile_2  = profile_raw_2
            else:
                selected_profile_2  = profile_dF_2
            arr_val_2, arr_var_2 = stat_dict[stat_method](selected_profile_2)

        # plotting
        time_line = np.linspace(0, (input_img_0.shape[0]-1)*time_scale, \
                                num=input_img_0.shape[0])
        
        mpl_fig = plt.figure()
        ax = mpl_fig.add_subplot(111)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)

        ax.grid(color='grey', linewidth=.25)
        ax.set_xlabel('Time, s')
        ax.set_ylabel(ylab)

        ax.errorbar(time_line, arr_val_0,
                    yerr = arr_var_0,
                    fmt ='-o', capsize=2, label=img_0.name,
                    alpha=0.75, color='black')
        
        if profiles_num == '2' or profiles_num == '3':
            ax.errorbar(time_line, arr_val_1,
                        yerr = arr_var_1,
                        fmt ='-o', capsize=2, label=img_1.name,
                        alpha=0.75, color='red')

        if profiles_num == '3':
            ax.errorbar(time_line, arr_val_2,
                        yerr = arr_var_2,
                        fmt ='-o', capsize=2, label=img_2.name,
                        alpha=0.75, color='blue')
        
        plt.legend()
        plt.title(f'{lab.name}, method {stat_method}')
        viewer.window.add_dock_widget(FigureCanvas(mpl_fig), name='Multiple Img Stat Prof.')


@magic_factory(call_button='Build Profiles',
               stat_method={"choices": ['se', 'iqr', 'ci']},
               labels_num={"choices": ['1', '2', '3']},)
def multi_labels_profile_stat(viewer: Viewer, img:Image,
                        lab_0:Labels, lab_1:Labels, lab_2:Labels,
                        labels_num:str='1',
                        absolute_intensity:bool=False, 
                        time_scale:float=5.0,
                        ΔF_win:int=5,
                        stat_method:str='se'):
    if input is not None:
        # mean, se
        arr_se_stat = lambda x: (np.mean(x, axis=0), \
                                 np.std(x, axis=0)/np.sqrt(x.shape[1]))
        # meadian, IQR
        arr_iqr_stat = lambda x: (np.median(x, axis=0), \
                                  stats.iqr(x, axis=0))
        # mean, CI
        arr_ci_stat = lambda x, alpha=0.05: (np.mean(x, axis=0), \
                                             stats.t.ppf(1-alpha/2, df=x.shape[1]-1) \
                                                         *np.std(x, axis=0, ddof=1)/np.sqrt(x.shape[1]))
        stat_dict = {'se':arr_se_stat,
                     'iqr':arr_iqr_stat,
                     'ci':arr_ci_stat}

        # processing
        input_img = img.data
        input_lab_0 = lab_0.data
        
        profile_dF_0, profile_raw_0 = masking.label_prof_arr(input_label=input_lab_0,
                                                             input_img_series=input_img,
                                                             f0_win=ΔF_win)
        if absolute_intensity:
            selected_profile_0  = profile_raw_0
            ylab = 'I'
        else:
            selected_profile_0  = profile_dF_0
            ylab = 'ΔF/F0'
        arr_val_0, arr_var_0 = stat_dict[stat_method](selected_profile_0)

        if labels_num == '2' or labels_num == '3':
            input_lab_1 = lab_1.data
            profile_dF_1, profile_raw_1 = masking.label_prof_arr(input_label=input_lab_1,
                                                                 input_img_series=input_img,
                                                                 f0_win=ΔF_win)
            if absolute_intensity:
                selected_profile_1  = profile_raw_1
            else:
                selected_profile_1  = profile_dF_1
            arr_val_1, arr_var_1 = stat_dict[stat_method](selected_profile_1)

        if labels_num == '3':
            input_lab_2 = lab_2.data
            profile_dF_2, profile_raw_2 = masking.label_prof_arr(input_label=input_lab_2,
                                                                 input_img_series=input_img,
                                                                 f0_win=ΔF_win)
            if absolute_intensity:
                selected_profile_2  = profile_raw_2
            else:
                selected_profile_2  = profile_dF_2
            arr_val_2, arr_var_2 = stat_dict[stat_method](selected_profile_2)

        # plotting
        time_line = np.linspace(0, (input_img.shape[0]-1)*time_scale, \
                                num=input_img.shape[0])
        
        mpl_fig = plt.figure()
        ax = mpl_fig.add_subplot(111)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)

        ax.grid(color='grey', linewidth=.25)
        ax.set_xlabel('Time, s')
        ax.set_ylabel(ylab)

        ax.errorbar(time_line, arr_val_0,
                    yerr = arr_var_0,
                    fmt ='-o', capsize=2, label=lab_0.name,
                    alpha=0.75, color='black')
        
        if labels_num == '2' or labels_num == '3':
            ax.errorbar(time_line, arr_val_1,
                        yerr = arr_var_1,
                        fmt ='-o', capsize=2, label=lab_1.name,
                        alpha=0.75, color='red')

        if labels_num == '3':
            ax.errorbar(time_line, arr_val_2,
                        yerr = arr_var_2,
                        fmt ='-o', capsize=2, label=lab_2.name,
                        alpha=0.75, color='blue')
        
        plt.legend()
        plt.title(f'{img.name}, method {stat_method}')
        viewer.window.add_dock_widget(FigureCanvas(mpl_fig), name='Multiple Lab Stat Prof.')


if __name__ == '__main__':

    import napari
    viewer = napari.Viewer()
    viewer = Viewer()

    split_channels_widget = split_channels()
    viewer.window.add_dock_widget(split_channels_widget, name = 'Preprocessing',
                                  area='right')