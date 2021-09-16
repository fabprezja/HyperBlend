"""
Desired folder structure for optimization process:

- project_root
    - optimization
        - set_name
            - target (refl tran)
                - target.toml
            - working_temp
                - rend_leaf
                - rend_refl_ref
                - rend_tran_ref
            - result
                - final_result.toml
                - plot
                    - result_plot.jpg
                    - parameter_a.png
                    - parameter_b.png
                    - ...
                - sub_result
                    - wl_1XXX.toml
                    - wl_2XXX.toml
                    - ...

For parallel processing

1. fetch wavelengths, and target reflectance and transmittance from optimization/target/target.toml before threading
2. deside a starting guess (constant?)
3. rend references to working_temp/rend_refl_ref and rend_tran_ref
4. make a thread pool
5. run wavelength-wise optimization
    5.1 rend leaf to rend_leaf folder
    5.2 retrieve r and t
    5.3 compare to target
    5.4 finish when good enough
    5.5 save result (a,b,c,d,r,t, and metadata) to sub_result/wl_XX.toml
6. collect subresults to single file (add RMSE and such)
7. plot resulting (a,b,c,d,r,t) 

"""

import math
import time
import logging
import multiprocessing
from multiprocessing import Pool

import scipy.optimize as optimize
from scipy.optimize import OptimizeResult
import numpy as np


from src import constants as C, utils
from src.render_parameters import RenderParametersForSingle
from src.render_parameters import RenderParametersForSeries
from src import blender_control as B
from src import data_utils as DU
from src import file_handling as FH
from src import toml_handlling as T
from src import plotter


# Bounds
# Do not let densities (x1,x2) drop to 0 as it will result in nonphysical behavior.
lb = [0.01, 0.01, -0.5, 0]
ub = [1, 1, 0.5, 1]
bounds = (lb, ub)
# Control how much density variables (x1,x2) are scaled for rendering. Value of 100 cannot
# produce r = 0 or t = 0. Produced values do not significantly change when greater than 300.
density_scale = 200
# Function value change tolerance for lsq minimization
ftol = 1e-2
# Absolute termination condition for basin hopping
ftol_abs = 1.0
# Variable value change tolerance for lsq minimization
xtol = 1e-5
# Stepsize for finite difference jacobian estimation. Smaller step gives
# better results, but the variables look cloudy. Big step is faster and variables
# smoother but there will be outliers in the results. Good stepsize is between 0.001 and 0.01.
diffstep = 0.005


def init(set_name: str, clear_subresults: bool):
    """Create empty folders etc."""

    FH.create_opt_folder_structure(set_name)
    FH.clear_rend_leaf(set_name)
    FH.clear_rend_refs(set_name)
    if clear_subresults:
        FH.clear_folder(FH.get_path_opt_subresult(set_name))


def run_optimization_in_batches(set_name: str, batch_n=1, opt_method='basin_hopping'):
    """
    Maybe better not to use this as it may cause some discontinuity in variable space.
    """

    wl_n = len(T.read_target(set_name))
    batch_size = int(wl_n / batch_n)
    step_size = int(wl_n/batch_size)
    for i in range(batch_n):
        selector = []
        for j in range(batch_size):
            selector.append(i+j*step_size)
        wls = T.read_target(set_name)[selector]
        print(f"Batch {i}: \n{wls}")
        run_optimization(set_name, wls, opt_method=opt_method)
    # do the last item for odd list
    if wl_n % batch_n != 0:
        selector = [wl_n-1]
        wls = T.read_target(set_name)[selector]
        print(f"Batch {i+1}: \n{wls}")
        run_optimization(set_name, wls, opt_method=opt_method)


def run_optimization(set_name: str, targets=None, use_threads=True, opt_method='basin_hopping', resolution=1):
    """Run optimization batch.

    Give targets as a batch. If none given, all target wls are run, except those excluded by resolution.

    :param set_name:
        Set name.
    :param targets:
        List of target wavelengths. If none given, the whole target list
        on disk is used. This is for running wavelengths in batches.
        TODO May behave incorrectly if resolution (other than 1) is given.
    :param use_threads:
        If True use parallel computation.
    :param opt_method:
        Optimization method to be used. Check implementation for available options.
    :param resolution:
        Spectral resolution. Default value 1 will optimize all wavelengths. Value 10
        would optimize every 10th spectral band.
    """

    total_time_start = time.perf_counter()

    if targets is None:
        targets = T.read_target(set_name)

    # Spectral resolution
    if resolution is not 1:
        targets = targets[0:-1:resolution]

    if use_threads:
        # Reformat target data
        # targets = utils.chunks(targets, len(targets))
        # for target in targets:
        param_list = [(a[0], a[1], a[2], set_name, opt_method) for a in targets]
        with Pool() as pool:
            pool.map(optimize_single_wl_threaded, param_list)
    else:
        for target in targets:
            wl = target[0]
            r_m = target[1]
            t_m = target[2]
            optimize_single_wl(wl, r_m, t_m, set_name, opt_method)

    logging.info("Finished optimizing of all wavelengths. Saving final result")
    elapsed_min = (time.perf_counter() - total_time_start) / 60.
    make_final_result(set_name, wall_clock_time_min=elapsed_min)

# NOTE not in use
# Optimize the whole spectra in single run, i.e., 8000 variables.
# This is not very efficient. A single render run is faster, but
# gradient estimation runs take forever as there are so many parameters.
# def optimize_spectrawise(targets, set_name: str, opt_method: str):
#     print(f"Fake spectrawise optimization")
#     print(f"target shape {targets.shape}")
#     wl_list = targets[:, 0]
#     rm_list = targets[:, 1]
#     tm_list = targets[:, 2]
#     print(f"wl_list shape {wl_list.shape}")
#     print(f"rm_list shape {rm_list.shape}")
#     print(f"tm_list shape {tm_list.shape}")
#
#     n = len(wl_list)
#
#     def RMSE(a,b):
#         diff = a-b
#         return np.sqrt(np.mean(diff*diff))
#
#     def SAM(a,b):
#         return np.arccos(a.dot(b) / (np.linalg.norm(a) * np.linalg.norm(b)))
#
#     def distance_spectral(r_list, t_list):
#         rmse_r = RMSE(r_list, rm_list)
#         rmse_t = RMSE(t_list, tm_list)
#         rmse = rmse_r + rmse_t
#         sam_r = SAM(r_list, rm_list)
#         sam_t = SAM(t_list, tm_list)
#         sam = sam_r + sam_t
#         return rmse + sam
#
#     def render_references():
#         rpfs = RenderParametersForSeries()
#         rpfs.clear_on_start = True
#         rpfs.clear_references = True
#         rpfs.render_references = True
#         rpfs.dry_run = False
#         rpfs.wl_list = wl_list
#         rpfs.abs_dens_list = np.ones_like(wl_list)
#         rpfs.scat_dens_list = np.ones_like(wl_list)
#         rpfs.scat_ai_list = np.zeros_like(wl_list)
#         rpfs.mix_fac_list = np.ones_like(wl_list)
#         B.run_render_series(rpfs, rend_base=FH.get_path_opt_working(set_name))
#
#     def f(x):
#         rpfs = RenderParametersForSeries()
#         rpfs.clear_on_start = True
#         rpfs.clear_references = False
#         rpfs.render_references = False
#         rpfs.dry_run = False
#         rpfs.wl_list = wl_list
#         rpfs.abs_dens_list = x[0:n] * density_scale
#         rpfs.scat_dens_list = x[n:2*n] * density_scale
#         rpfs.scat_ai_list = x[2*n:3*n]
#         rpfs.mix_fac_list = x[3*n:4*n]
#         B.run_render_series(rpfs, rend_base=FH.get_path_opt_working(set_name))
#         r_list = DU.get_relative_refl_or_tran_series(C.imaging_type_refl, rpfs.wl_list, base_path=FH.get_path_opt_working(set_name))
#         t_list = DU.get_relative_refl_or_tran_series(C.imaging_type_tran, rpfs.wl_list, base_path=FH.get_path_opt_working(set_name))
#         plotter.plot_refl_tran_as_subresult(set_name, 'intermediate_run', wl_list,x[0:n],x[n:2*n],x[2*n:3*n],x[3*n:4*n],r_list
#                                             ,t_list,rm_list,tm_list)
#         return distance_spectral(r_list, t_list)
#
#     render_references()
#     x1 = np.ones_like(wl_list) * 0.2
#     x2 = np.ones_like(wl_list) * 0.7
#     x3 = np.ones_like(wl_list) * 0.3
#     x4 = np.ones_like(wl_list) * 0.6
#     X_0 = np.array([item for sublist in [x1,x2,x3,x4] for item in sublist])
#
#     # Reasonable starting guess that shows the main features in reflectance and transmittance
#     # X_0 = [0.50938556, 0.4696383 , 0.44360945, 0.30344762, 0.3595715 ,
#     #    0.4132276 , 0.29661224, 0.15289022, 0.16114292, 0.15732321,
#     #    0.15110985, 0.15611187, 0.15597202, 0.15013823, 0.15055638,
#     #    0.14804305, 0.14757165, 0.14845163, 0.14896373, 0.15734285,
#     #    0.25935591, 0.31425254, 0.28753012, 0.24181371, 0.18674444,
#     #    0.17679054, 0.1794984 , 0.18593237, 0.19029145, 0.19646252,
#     #    0.40726481, 0.43392185, 0.39644617, 0.34210611, 0.30912576,
#     #    0.26426778, 0.27206423, 0.27566668, 0.51643512, 0.53208483,
#     #    0.55373487, 0.62688732, 0.60233993, 0.56661272, 0.61940756,
#     #    0.77895249, 0.7905144 , 0.79133406, 0.78380967, 0.78810893,
#     #    0.78792502, 0.7982574 , 0.79696449, 0.77427406, 0.75951097,
#     #    0.76303765, 0.76289609, 0.74196504, 0.66934032, 0.62474713,
#     #    0.63980387, 0.68923463, 0.71323151, 0.72149813, 0.72200398,
#     #    0.71175309, 0.70517111, 0.68884837, 0.56970119, 0.55225628,
#     #    0.57741962, 0.60650591, 0.62676496, 0.61078179, 0.64024107,
#     #    0.6458653 , 0.12012326, 0.10724353, 0.12700202, 0.1719956 ,
#     #    0.17849767, 0.16360968, 0.1799004 , 0.23674973, 0.25975352,
#     #    0.2782397 , 0.29461689, 0.31399409, 0.3155561 , 0.31964326,
#     #    0.32429784, 0.31931434, 0.32354542, 0.32672714, 0.32676146,
#     #    0.32630545, 0.25444745, 0.25367128, 0.25562067, 0.29721647,
#     #    0.3166608 , 0.32348498, 0.33059366, 0.32819778, 0.32419809,
#     #    0.3217571 , 0.1864581 , 0.16266624, 0.20818866, 0.26449421,
#     #    0.29837181, 0.31338974, 0.31726004, 0.32231329, 0.23490195,
#     #    0.27378473, 0.3075299 , 0.46375067, 0.40977038, 0.34041586,
#     #    0.45680987, 0.76432223, 0.78784698, 0.79287298, 0.78871258,
#     #    0.79044131, 0.79051313, 0.79669438, 0.79542437, 0.7636586 ,
#     #    0.7476339 , 0.75742052, 0.7492562 , 0.71483554, 0.52421548,
#     #    0.45638907, 0.48585699, 0.57956303, 0.63630049, 0.65822823,
#     #    0.65846418, 0.63338614, 0.6200738 , 0.59350277, 0.34010804,
#     #    0.31185644, 0.361193  , 0.41818209, 0.46775955, 0.46311834,
#     #    0.48761989, 0.49036128]
#
#     lb1 = np.ones_like(wl_list) * lb[0]
#     lb2 = np.ones_like(wl_list) * lb[1]
#     lb3 = np.ones_like(wl_list) * lb[2]
#     lb4 = np.ones_like(wl_list) * lb[3]
#     low_bound = np.array([item for sublist in [lb1,lb2,lb3,lb4] for item in sublist])
#     ub1 = np.ones_like(wl_list) * ub[0]
#     ub2 = np.ones_like(wl_list) * ub[1]
#     ub3 = np.ones_like(wl_list) * ub[2]
#     ub4 = np.ones_like(wl_list) * ub[3]
#     up_bound = np.array([item for sublist in [ub1, ub2, ub3, ub4] for item in sublist])
#     spectral_bounds = (low_bound, up_bound)
#
#     res = optimize.least_squares(f, X_0, bounds=spectral_bounds, method='trf', verbose=2, gtol=None,
#                                  diff_step=diffstep, ftol=ftol, xtol=xtol)
#     print(res)


def optimize_single_wl_threaded(args):
    optimize_single_wl(args[0], args[1], args[2], args[3], args[4])


def optimize_single_wl(wl: float, r_m: float, t_m: float, set_name: str, opt_method: str):
    """Optimize stuff"""

    print(f'Optimizing wavelength {wl} nm started.', flush=True)

    if FH.subresult_exists(set_name, wl):
        print(f"Subresult for wl {wl:.2f} already exists. Skipping optimization.", flush=True)
        return

    start = time.perf_counter()
    history = []

    def distance(r, t):
        r_diff = (r - r_m)
        t_diff = (t - t_m)
        dist = math.sqrt(r_diff * r_diff + t_diff * t_diff)
        return dist

    def f(x):
        """Function to be minimized F = sum(d_i²)."""

        rps = RenderParametersForSingle()
        rps.clear_rend_folder = False
        rps.clear_references = False
        rps.render_references = False
        rps.dry_run = False
        rps.wl = wl
        rps.abs_dens = x[0] * density_scale
        rps.scat_dens = x[1] * density_scale
        rps.scat_ai = x[2]
        rps.mix_fac = x[3]
        B.run_render_single(rps, rend_base=FH.get_path_opt_working(set_name))

        r = DU.get_relative_refl_or_tran(C.imaging_type_refl, rps.wl, base_path=FH.get_path_opt_working(set_name))
        t = DU.get_relative_refl_or_tran(C.imaging_type_tran, rps.wl, base_path=FH.get_path_opt_working(set_name))
        # Debug print
        # print(f"rendering with x = {printable_variable_list(x)} resulting r = {r:.3f}, t = {t:.3f}")
        dist = distance(r, t) * density_scale
        # tikhonov = 10
        # x_norm = np.linalg.norm(x*tikhonov)
        history.append([*x, r, t])

        penalty = 0
        some_big_number = 1e6
        if r+t > 1:
            penalty = some_big_number
        return dist + penalty #+x_norm


    # Do this once to render references
    rps = RenderParametersForSingle()
    rps.render_references = True
    rps.clear_rend_folder = False
    rps.clear_references = False
    rps.dry_run = False
    rps.wl = wl
    rps.abs_dens = 0
    rps.scat_dens = 0
    rps.scat_ai = 0
    rps.mix_fac = 0
    B.run_render_single(rps, rend_base=FH.get_path_opt_working(set_name))

    # previous_wl = wl-resolution
    # if FH.subresult_exists(set_name, previous_wl):
    #     adjacent_result = T.read_subresult(set_name, previous_wl)
    #     a = adjacent_result[C.subres_key_history_absorption_density][-1]
    #     b = adjacent_result[C.subres_key_history_scattering_density][-1]
    #     c = adjacent_result[C.subres_key_history_scattering_anisotropy][-1]
    #     d = adjacent_result[C.subres_key_history_mix_factor][-1]
    #     print(f"Using result of previous wl ({previous_wl}) as a starting guess.", flush=True)
    # else:
    #     a = 0.5
    #     b = 0.5
    #     c = 0.2
    #     d = 0.5


    # x_0 = [a,b,c,d]
    # x_0 =  [0.21553118, 2.28501613, 0.45281115, 0.50871691]
    x_0 = get_starting_guess(1-(r_m + t_m))
    print(f"wl ({wl:.2f})x_0: {x_0}", flush=True)

    history.append([*x_0, 0.0, 0.0])
    seed = 123

    print(f'optimizing with {opt_method}', flush=True)
    if opt_method == 'least_squares':
        res = optimize.least_squares(f, x_0,  bounds=bounds, method='dogbox', verbose=2, gtol=None,
                                     diff_step=diffstep, ftol=ftol, xtol=xtol)
    elif opt_method == 'shgo':
        shgo_bounds = [(b[0], b[1]) for b in zip(lb, ub)]
        res = optimize.shgo(f, shgo_bounds, iters=10, n=2, sampling_method='sobol')
        print(f'result: \n{res}', flush=True)
    elif opt_method == 'anneal':
        anneal_bounds = list(zip(lb, ub))
        res = optimize.dual_annealing(f, anneal_bounds, seed=seed, maxiter=500, maxfun=1000, initial_temp=5000,
                                      x0=x_0, restart_temp_ratio=0.9999, visit=2.1, accept=-9000)
        print(f'result: \n{res}', flush=True)
    elif opt_method == 'basin_hopping':
        class Stepper(object):

            def __init__(self, stepsize=0.1):
                self.stepsize = stepsize

            def __call__(self, x):

                for i in range(len(x)):
                    bound_length = ub[i] - lb[i]
                    s = bound_length * self.stepsize # max stepsize as percentage
                    x[i] += np.random.uniform(-s, s)
                    if x[i] > ub[i]:
                        x[i] = ub[i]
                    if x[i] < lb[i]:
                        x[i] = lb[i]

                return x

        def callback(x, f, accepted):
            # print("####Callback message########")
            # print(x)
            # print(f)
            # print(accepted)
            # print("############################")
            if f <= ftol_abs:
                return True

        def custom_local_minimizer(fun, x0, args=(), maxfev=None, stepsize=0.1, maxiter=100, callback=None, **options):
            res_lsq = optimize.least_squares(fun, x0, bounds=bounds, method='dogbox', verbose=2,
                                         gtol=None, diff_step=diffstep, ftol=ftol, xtol=xtol)
            return res_lsq

        custom_step = Stepper()
        minimizer_options = None
        minimizer_kwargs = {'bounds': bounds, 'options': minimizer_options, 'method': custom_local_minimizer}
        res = optimize.basinhopping(f, x0=x_0, stepsize=0.1, niter=2, T=0, interval=1,
                                    take_step=custom_step, callback=callback, minimizer_kwargs=minimizer_kwargs)
        print(f'basing hopping result: \n{res}', flush=True)
    else:
        raise Exception(f"Optimization method '{opt_method}' not recognized.")
    elapsed = time.perf_counter() - start

    # TODO temporary solution: add one extra element to the end of the history
    # to place the best value at the end even if used optimizer does not converge
    f(res.x)

    res_dict = {
        C.subres_key_wl: wl,
        C.subres_key_reflectance_measured: r_m,
        C.subres_key_transmittance_measured: t_m,
        C.subres_key_reflectance_modeled: history[-1][4],
        C.subres_key_transmittance_modeled: history[-1][5],
        C.subres_key_reflectance_error: math.fabs(history[-1][4] - r_m),
        C.subres_key_transmittance_error: math.fabs(history[-1][5] - t_m),
        C.subres_key_iterations: len(history) - 1,
        C.subres_key_optimizer: opt_method,
        C.subres_key_optimizer_ftol: ftol,
        C.subres_key_optimizer_xtol: xtol,
        C.subres_key_optimizer_diffstep: diffstep,
        C.subres_key_optimizer_result: res,
        C.subres_key_elapsed_time_s: elapsed,
        C.subres_key_history_reflectance: [float(h[4]) for h in history],
        C.subres_key_history_transmittance: [float(h[5]) for h in history],
        C.subres_key_history_absorption_density: [float(h[0]) for h in history],
        C.subres_key_history_scattering_density: [float(h[1]) for h in history],
        C.subres_key_history_scattering_anisotropy: [float(h[2]) for h in history],
        C.subres_key_history_mix_factor: [float(h[3]) for h in history],
    }
    # print(res_dict)
    logging.info(f'Optimizing wavelength {wl} nm finished. Writing subesult and plot to disk.')

    T.write_subresult(set_name, res_dict)
    # Save the plot of optimization history
    # Plotter can re-create the plots from saved toml data, so there's no need to
    # run the whole optimization just to change the images.
    plotter.plot_subresult_opt_history(set_name, wl, save_thumbnail=True, dont_show=True)


def make_final_result(set_name:str, wall_clock_time_min=0.0):
    """
    :param set_name:
        Set name.
    :param wall_clock_time_min:
        Wall clock time may differ from summed subresult time if computed in parallel.
    """

    # Collect subresults
    subreslist = T.collect_subresults(set_name)
    result_dict = {}

    # Set starting value to which earlier result time is added.
    result_dict[C.result_key_wall_clock_elapsed_min] = wall_clock_time_min

    try:
        previous_result = T.read_final_result(set_name)  # throws OSError upon failure
        this_result_time = result_dict[C.result_key_wall_clock_elapsed_min]
        previous_result_time = previous_result[C.result_key_wall_clock_elapsed_min]
        result_dict[C.result_key_wall_clock_elapsed_min] = this_result_time + previous_result_time
    except OSError as e:
        pass  # this is ok for the first round

    result_dict[C.result_key_process_elapsed_min] = np.sum(subres[C.subres_key_elapsed_time_s] for subres in subreslist) / 60.0
    result_dict[C.result_key_r_RMSE] = np.sqrt(np.mean(np.array([subres[C.subres_key_reflectance_error] for subres in subreslist])**2))
    result_dict[C.result_key_t_RMSE] = np.sqrt(np.mean(np.array([subres[C.subres_key_transmittance_error] for subres in subreslist])**2))
    result_dict[C.subres_key_optimizer] = subreslist[0][C.subres_key_optimizer],
    result_dict[C.subres_key_optimizer_ftol] = ftol,
    result_dict[C.subres_key_optimizer_xtol] = xtol,
    result_dict[C.subres_key_optimizer_diffstep] = diffstep,
    if result_dict[C.subres_key_optimizer][0] == 'basin_hopping':
        result_dict['basin_iterations_required'] = sum([(subres[C.subres_key_optimizer_result]['nit'] > 1) for subres in subreslist])
    result_dict[C.result_key_wls] = [subres[C.subres_key_wl] for subres in subreslist]
    result_dict[C.result_key_refls_modeled] = [subres[C.subres_key_reflectance_modeled] for subres in subreslist]
    result_dict[C.result_key_refls_measured] = [subres[C.subres_key_reflectance_measured] for subres in subreslist]
    result_dict[C.result_key_refls_error] = [subres[C.subres_key_reflectance_error] for subres in subreslist]
    result_dict[C.result_key_trans_modeled] = [subres[C.subres_key_transmittance_modeled] for subres in subreslist]
    result_dict[C.result_key_trans_measured] = [subres[C.subres_key_transmittance_measured] for subres in subreslist]
    result_dict[C.result_key_trans_error] = [subres[C.subres_key_transmittance_error] for subres in subreslist]

    result_dict[C.result_key_absorption_density] = [subres[C.subres_key_history_absorption_density][-1] for subres in subreslist]
    result_dict[C.result_key_scattering_density] = [subres[C.subres_key_history_scattering_density][-1] for subres in subreslist]
    result_dict[C.result_key_scattering_anisotropy] = [subres[C.subres_key_history_scattering_anisotropy][-1] for subres in subreslist]
    result_dict[C.result_key_mix_factor] = [subres[C.subres_key_history_mix_factor][-1] for subres in subreslist]

    T.write_final_result(set_name, result_dict)
    plotter.plot_final_result(set_name, save_thumbnail=True, dont_show=True)


def printable_variable_list(as_array):
    l = [f'{variable:.3f}' for variable in as_array]
    return l

def get_starting_guess(absorption:float):
    """
    Gives starting guess for given absorption.
    """
    def f(coeffs):
        return coeffs[2]*absorption*absorption + coeffs[1]*absorption + coeffs[0]

    absorption_density = [0.15319704, 0.13493788, 0.43538607]
    scattering_density = [ 0.59922746, -0.0009426, -0.31473394]
    scattering_anisotropy = [ 0.29456347, -0.24329242, 0.14122699]
    mix_factor = [0.793028, 0.2839754, -0.88555556]
    return [f(absorption_density), f(scattering_density), f(scattering_anisotropy), f(mix_factor)]
