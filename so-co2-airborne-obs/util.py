import os
import sys
import time
import shutil
import yaml
import hashlib
import warnings

from collections.abc import Iterable
from itertools import product 

import statsmodels.api as sm
from scipy import stats
import scipy.odr as odr

import calendar
import cftime

import cartopy.crs as ccrs

import numpy as np
import xarray as xr
import pandas as pd

import ESMF

import matplotlib.pyplot as plt
import matplotlib.path as mpath
import matplotlib.gridspec as gridspec

path_to_here = os.path.dirname(os.path.realpath(__file__))
so_lat_slice = slice(-80, -45)

CO2_SF6_emission_ratio = 14.5

# with open(f'{path_to_here}/data/named-points.yaml', 'r') as fid:
#     named_points = yaml.safe_load(fid)
    
season_yearfrac = dict(
    DJF=0.04, MAM=0.287, JJA=0.538, SON=0.790,
)


def _gen_plotname(plot_name, dirout):
    """generate a name for plotting"""
    plot_basename, ext = os.path.splitext(plot_name)
    
    if os.path.exists('_figure-order.yml'):
        with open('_figure-order.yml') as fid:
            fig_map = yaml.safe_load(fid)
        assert len(set(fig_map.values())) == len(fig_map.values()), (
            'non-unique figure names found in _figure-order.yml'
        )
        
        fig_key = None
        for key, value in fig_map.items():
            if plot_basename == value:
                fig_key = key 
                break
                
        if fig_key is not None:
            return f'{dirout}/Fig-{fig_key}-{plot_basename}'
        else:
            dirout = f'{dirout}/misc'
            os.makedirs(dirout, exist_ok=True)
            return f'{dirout}/{plot_basename}'    


def savefig(plot_name):
    """Write figure"""
    
    if 'TEST_PLOT_FIGURE_DIR' in os.environ:
        dirout = os.environ['TEST_PLOT_FIGURE_DIR']
    else:
        dirout = 'figures'    
    os.makedirs(dirout, exist_ok=True)
    
    plot_basename = _gen_plotname(plot_name, dirout)
    
    for ext in ['pdf', 'png']:
        plt.savefig(f'{plot_basename}.{ext}', 
                    dpi=300, 
                    bbox_inches='tight', 
                    metadata={'CreationDate': None})

    
def haversine(lon1, lat1, lon2, lat2):
    """
    Calculate the great circle distance between two points
    on the earth (specified in decimal degrees)
    lon1, lat1 := scalars
    lon2, lat2 := 1D arrays
    """

    Re = 6378.137

    # convert decimal degrees to radians
    deg2rad = np.pi / 180.
    lon1 = np.array(lon1) * deg2rad
    lat1 = np.array(lat1) * deg2rad
    lon2 = np.array(lon2) * deg2rad
    lat2 = np.array(lat2) * deg2rad

    if lon2.shape:
        N = lon2.shape[0]
        lon1 = np.repeat(lon1, N)
        lat1 = np.repeat(lat1, N)

    # haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat / 2.)**2. + np.cos(lat1) * \
        np.cos(lat2) * np.sin(dlon / 2.)**2.
    c = 2. * np.arcsin(np.sqrt(a))
    km = Re * c
    return km


def points_in_range(clon, clat, plon, plat, range_km):
    """Find points within range of a point."""
    if hasattr(clon, '__len__'):
        m = np.zeros(plon.shape, dtype=bool)
        for cx, cy in zip(clon, clat):
            mi = points_in_range(cx, cy, plon, plat, range_km)
            m = (m | mi)
    else:
        mask = np.array(
            (haversine(
                clon,
                clat,
                plon.ravel(),
                plat.ravel()) <= range_km))
        if mask.ndim != plon.ndim:
            m = mask.reshape(plon.shape)
        else:
            m = mask
    return m


def is_p_inside_points_hull(points, p):
    """Return points inside convex hull."""

    from scipy.spatial import ConvexHull

    hull = ConvexHull(points)
    new_points = np.append(points, p, axis=0)
    new_hull = ConvexHull(new_points)
    if list(hull.vertices) == list(new_hull.vertices):
        return True
    else:
        return False


def griddata(x, y, z, xgrid_edges, ygrid_edges, use_rbf=True, smooth=0, trim_to_hull=True):
    """Bin data to a 2D grid and interpolate with a radial basis function
       interpolant.
    """

    import scipy.interpolate

    # get rid of missing values
    mask = np.isnan(x) | np.isnan(y) | np.isnan(z)
    x = x[~mask]
    y = y[~mask]
    z = z[~mask]

    # bin the data and compute means within each bin
    N, _, _ = np.histogram2d(x, y, bins=[xgrid_edges, ygrid_edges], density=False)
    S, _, _ = np.histogram2d(x, y, bins=[xgrid_edges, ygrid_edges], weights=z, density=False)
    N[N == 0] = np.nan
    ZI = S / N

    # compute grid-cell centers
    xi = np.vstack((xgrid_edges[:-1], xgrid_edges[1:])).mean(axis=0)
    yi = np.vstack((ygrid_edges[:-1], ygrid_edges[1:])).mean(axis=0)
    XI, YI = np.meshgrid(xi, yi)
    ZI = ZI.transpose()
    N = N.transpose()
    
    if not use_rbf:
        return XI, YI, ZI, N
    
    # interpolate
    xb = XI.ravel()[~np.isnan(ZI.ravel())]
    yb = YI.ravel()[~np.isnan(ZI.ravel())]
    zb = ZI.ravel()[~np.isnan(ZI.ravel())]

    rbf = scipy.interpolate.Rbf(xb, yb, zb, function='linear', smooth=smooth)
    ZI = rbf(XI, YI)

    # trim the intoplated field with convex hull of input points
    if trim_to_hull:
        points = np.array([x, y]).transpose()
        p = np.empty((1, 2))
        for i in range(len(xi)):
            for j in range(len(yi)):
                p[0, :] = [XI[j, i], YI[j, i]]
                if not is_p_inside_points_hull(points, p):
                    ZI[j, i] = np.nan

    return XI, YI, ZI, N


def esmf_create_grid(xcoords, ycoords, xcorners=False, ycorners=False,
                corners=False, domask=False, doarea=False,
                ctk=ESMF.TypeKind.R8):
    """
    Create a 2 dimensional Grid using the bounds of the x and y coordiantes.
    :param xcoords: The 1st dimension or 'x' coordinates at cell centers, as a Python list or numpy Array
    :param ycoords: The 2nd dimension or 'y' coordinates at cell centers, as a Python list or numpy Array
    :param xcorners: The 1st dimension or 'x' coordinates at cell corners, as a Python list or numpy Array
    :param ycorners: The 2nd dimension or 'y' coordinates at cell corners, as a Python list or numpy Array
    :param domask: boolean to determine whether to set an arbitrary mask or not
    :param doarea: boolean to determine whether to set an arbitrary area values or not
    :param ctk: the coordinate typekind
    :return: grid
    """
    [x, y] = [0, 1]

    # create a grid given the number of grid cells in each dimension, the center stagger location is allocated, the
    # Cartesian coordinate system and type of the coordinates are specified
    max_index = np.array([len(xcoords), len(ycoords)])
    grid = ESMF.Grid(max_index,
                     staggerloc=[ESMF.StaggerLoc.CENTER],
                     coord_sys=ESMF.CoordSys.SPH_DEG,
                     num_peri_dims=1,
                     periodic_dim=x,
                     coord_typekind=ctk)

    # set the grid coordinates using pointer to numpy arrays, parallel case is handled using grid bounds
    gridXCenter = grid.get_coords(x)
    x_par = xcoords[grid.lower_bounds[ESMF.StaggerLoc.CENTER][x]:grid.upper_bounds[ESMF.StaggerLoc.CENTER][x]]

    gridXCenter[...] = x_par.reshape((x_par.size, 1))

    gridYCenter = grid.get_coords(y)
    y_par = ycoords[grid.lower_bounds[ESMF.StaggerLoc.CENTER][y]:grid.upper_bounds[ESMF.StaggerLoc.CENTER][y]]
    gridYCenter[...] = y_par.reshape((1, y_par.size))

    # create grid corners in a slightly different manner to account for the bounds format common in CF-like files
    if corners:
        raise ValueError('not tested')
        grid.add_coords([ESMF.StaggerLoc.CORNER])
        lbx = grid.lower_bounds[ESMF.StaggerLoc.CORNER][x]
        ubx = grid.upper_bounds[ESMF.StaggerLoc.CORNER][x]
        lby = grid.lower_bounds[ESMF.StaggerLoc.CORNER][y]
        uby = grid.upper_bounds[ESMF.StaggerLoc.CORNER][y]

        gridXCorner = grid.get_coords(x, staggerloc=ESMF.StaggerLoc.CORNER)
        for i0 in range(ubx - lbx - 1):
            gridXCorner[i0, :] = xcorners[i0+lbx, 0]
        gridXCorner[i0 + 1, :] = xcorners[i0+lbx, 1]

        gridYCorner = grid.get_coords(y, staggerloc=ESMF.StaggerLoc.CORNER)
        for i1 in range(uby - lby - 1):
            gridYCorner[:, i1] = ycorners[i1+lby, 0]
        gridYCorner[:, i1 + 1] = ycorners[i1+lby, 1]

    # add an arbitrary mask
    if domask:
        raise ValueError('not tested')
        mask = grid.add_item(ESMF.GridItem.MASK)
        mask[:] = 1
        mask[np.where((1.75 <= gridXCenter.any() < 2.25) &
                      (1.75 <= gridYCenter.any() < 2.25))] = 0

    # add arbitrary areas values
    if doarea:
        raise ValueError('not tested')
        area = grid.add_item(ESMF.GridItem.AREA)
        area[:] = 5.0

    return grid


def esmf_create_locstream_spherical(lon, lat, coord_sys=ESMF.CoordSys.SPH_DEG,
                                    mask=None):
    """
    :param coord_sys: the coordinate system of the LocStream
    :param domask: a boolean to tell whether or not to add a mask
    :return: LocStream
    """
    if ESMF.pet_count() is not 1:
        raise ValueError("processor count must be 1 to use this function")

    locstream = ESMF.LocStream(len(lon), coord_sys=coord_sys)

    locstream["ESMF:Lon"] = lon
    locstream["ESMF:Lat"] = lat
    if mask is not None:
        locstream["ESMF:Mask"] = mask.astype(np.int32)

    return locstream


def esmf_interp_points(ds_in, locs_lon, locs_lat, lon_field_name='lon',
                lat_field_name='lat'):
    """Use ESMF toolbox to interpolate grid at points."""

    # generate grid object
    grid = esmf_create_grid(ds_in[lon_field_name].values.astype(np.float),
                            ds_in[lat_field_name].values.astype(np.float))


    # generate location stream object
    locstream = esmf_create_locstream_spherical(locs_lon.values.astype(np.float),
                                                locs_lat.values.astype(np.float))
    
    # generate regridding object
    srcfield = ESMF.Field(grid, name='srcfield')
    dstfield = ESMF.Field(locstream, name='dstfield')
    regrid = ESMF.Regrid(srcfield, dstfield,
                         regrid_method=ESMF.RegridMethod.BILINEAR,
                         unmapped_action=ESMF.UnmappedAction.ERROR)

    # construct output dataset
    coords = {c: locs_lon[c] for c in locs_lon.coords}
    dims_loc = locs_lon.dims
    nlocs = len(locs_lon)
    ds_out = xr.Dataset(coords=coords, attrs=ds_in.attrs)

    for name, da_in in ds_in.data_vars.items():

        # get the dimensions of the input dataset; check if it's spatial
        dims_in = da_in.dims
        if lon_field_name not in dims_in or lat_field_name not in dims_in:
            continue

        # get the dimension/shape of output
        non_lateral_dims = dims_in[:-2]
        dims_out = non_lateral_dims + dims_loc
        shape_out = da_in.shape[:-2] + (nlocs,)

        # create output dataset
        da_out = xr.DataArray((np.ones(shape_out)*np.nan).astype(da_in.dtype),
                              name=name,
                              dims=dims_out,
                              attrs=da_in.attrs,
                              coords={c: da_in.coords[c] for c in da_in.coords
                                      if c in non_lateral_dims})
        dstfield.data[...] = np.nan

        if len(non_lateral_dims) > 0:
            da_in_stack = da_in.stack(non_lateral_dims=non_lateral_dims)
            da_out_stack = xr.full_like(da_out, fill_value=np.nan).stack(non_lateral_dims=non_lateral_dims)

            for i in range(da_in_stack.shape[-1]):
                srcfield.data[...] = da_in_stack.data[:, :, i].T
                dstfield = regrid(srcfield, dstfield, zero_region=ESMF.Region.SELECT)
                da_out_stack.data[:, i] = dstfield.data

            da_out.data = da_out_stack.unstack('non_lateral_dims').transpose(*dims_out).data

        else:
            srcfield.data[...] = da_in.data[:, :].T
            dstfield = regrid(srcfield, dstfield, zero_region=ESMF.Region.SELECT)
            da_out.data = dstfield.data

        ds_out[name] = da_out
        
    return ds_out


def infer_lat_name(ds):
    lat_names = ['latitude', 'lat']
    for n in lat_names:
        if n in ds:
            return n
    raise ValueError('could not determine lat name')    


def infer_lon_name(ds):
    lon_names = ['longitude', 'lon']
    for n in lon_names:
        if n in ds:
            return n
    raise ValueError('could not determine lon name')       

    
def lat_weights_regular_grid(lat):
    """
    Generate latitude weights for equally spaced (regular) global grids.
    Weights are computed as sin(lat+dlat/2)-sin(lat-dlat/2) and sum to 2.0.
    """   
    dlat = np.abs(np.diff(lat))
    np.testing.assert_almost_equal(dlat, dlat[0])
    w = np.abs(np.sin(np.radians(lat + dlat[0] / 2.)) - np.sin(np.radians(lat - dlat[0] / 2.)))

    if np.abs(lat[0]) > 89.9999: 
        w[0] = np.abs(1. - np.sin(np.radians(np.pi / 2 - dlat[0])))

    if np.abs(lat[-1]) > 89.9999:
        w[-1] = np.abs(1. - np.sin(np.radians(np.pi / 2 - dlat[0])))

    return w


def compute_grid_area(ds, check_total=True):
    """Compute the area of grid cells.
    
    Parameters
    ----------
    
    ds : xarray.Dataset
      Input dataset with latitude and longitude fields
    
    check_total : Boolean, optional
      Test that total area is equal to area of the sphere.
      
    Returns
    -------
    
    area : xarray.DataArray
       DataArray with area field.
    
    """
    
    radius_earth = 6.37122e6 # m, radius of Earth
    area_earth = 4.0 * np.pi * radius_earth**2 # area of earth [m^2]e
    
    lon_name = infer_lon_name(ds)       
    lat_name = infer_lat_name(ds)        
    
    weights = lat_weights_regular_grid(ds[lat_name])
    area = weights + 0.0 * ds[lon_name] # add 'lon' dimension
    area = (area_earth / area.sum(dim=(lat_name, lon_name))) * area
    
    if check_total:
        np.testing.assert_approx_equal(np.sum(area), area_earth)
        
    return xr.DataArray(area, dims=(lat_name, lon_name), attrs={'units': 'm^2', 'long_name': 'area'})  

    
def sum_da(ds, fields):
    total = xr.full_like(ds[fields[0]], fill_value=0.)
    for v in fields:
        total += ds[v]
    return total


def djfer(ds):
    """Compute DJF mean."""
    return ds.groupby('time.season').mean('time').sel(season='DJF')


def jjaer(ds):
    """Compute JJA mean."""
    return ds.groupby('time.season').mean('time').sel(season='JJA')


def monthly_climatology(ds):
    """Change to monthly frequency."""
    return ds.groupby('time.month').mean('time')


def day_of_year_noleap(dates):
    """Convert dates to Day of Year (omitting leap days)."""
    d0 = np.datetime64('2001-01-01') - 1
    doy_list = []
    months = (dates.astype('datetime64[M]').astype(int) % 12 + 1).astype(int)
    days = (dates.astype('datetime64[D]') - dates.astype('datetime64[M]') + 1).astype(int)
    for mm, dd in zip(months, days):
        if mm == 2 and dd == 29:
            doy = 0
        else:
            d = np.datetime64(f'2001-{mm:02d}-{dd:02d}')
            doy = ((d - d0) / np.timedelta64(1, 'D')).astype(int)
        doy_list.append(doy)

    return doy_list


def day_of_year(dates):
    """Convert dates to Day of Year."""   
    years = dates.astype('datetime64[Y]').astype(int) + 1970
    day0 = [np.datetime64(f'{y:04}-01-01') - 1 for y in years]
    return ((dates - day0) / np.timedelta64(1, 'D')).astype(int)


def daily_climatology(ds):
    """Compute a day-of-year climatology."""

    dso = xr.Dataset()
    ds = ds.copy()
    doy = day_of_year_noleap(ds.time.values)
    ds['doy'] = xr.DataArray(doy, dims=('time'))

    # copy coords
    for v in ds.coords:
        if 'time' not in ds[v].dims:
            dso[v] = ds[v].copy()
    
    first_var = True
    for v in ds.variables:
        if 'time' not in ds[v].dims:
            dso[v] = ds[v].copy()
            continue            
        elif v not in ['doy', 'time']:
            shape = list(ds[v].shape)
            dims = ds[v].dims
            shape[0] = 365

            dso[v] = xr.DataArray(np.empty(shape), dims=dims)
            count = np.zeros((365,))
            for doy, idx in ds.groupby('doy').groups.items():
                if doy == 0:
                    if first_var:
                        print('skipping leap days')
                else:
                    count[doy-1] += len(idx)
                    dso[v].data[doy-1,...] = ds[v].isel(time=idx).mean('time')
                    
            first_var = False

        dso['time'] = xr.DataArray(np.arange(1, 366, 1), dims=('time'))

    return dso


def antyear_daily(x, y):
    """rearrange time to put austral summer in the middle."""
    if isinstance(x, xr.DataArray):
        x = x.values
    
    jfmamj = x < 182.
    jasond = x >= 182.
    
    x_jasond = []
    y_jasond = []
    if any(jasond):
        x_jasond = x[jasond] - 181
        y_jasond = y[jasond]

    x_jfmamj = []
    y_jfmamj = []
    if any(jfmamj):
        x_jfmamj = x[jfmamj] + 184
        y_jfmamj = y[jfmamj]

    xout = np.concatenate([xi for xi in [x_jasond, x_jfmamj] if len(xi)])
    yout = np.concatenate([yi for yi in [y_jasond, y_jfmamj] if len(yi)])

    return xout, yout


def antyear_monthly(*args):
    if len(args) == 1:
        return _antyear_monthly_y(args[0])
    else:
        x = args[0]
        y = args[1]
        jfmamj = x <= 6
        jasond = x > 6
        oargs = [np.concatenate((x[jasond]-6, x[jfmamj]+6))]
        for arg in args[1:]:
            oargs.append(np.concatenate((arg[jasond], arg[jfmamj])))
            
        return tuple(oargs)


def _antyear_monthly_y(y):
    jfmamj = slice(0, 6)
    jasond = slice(6, 12)
    return np.concatenate((y[jasond], y[jfmamj]))


def mavg_periodic(x, n):
    nx = len(x)
    x = np.concatenate((x, x, x))
    return np.convolve(x, np.ones((n,))/n, mode='same')[nx:nx*2]


def mavg_periodic_ds(dset, n):   
    dso = dset.copy()
    for v in dset.data_vars:
        if 'time' in dset[v].dims and v != 'time':
            non_time_dims = [d for d in dset[v].dims if d != 'time']
            if non_time_dims:
                data = dset[v].stack(non_time_dims=non_time_dims)
                data_out = data.copy()
                for i in range(len(data.non_time_dims)):
                    data_out[:, i] = mavg_periodic(data[:, i], n)
                dso[v].data = data_out.unstack('non_time_dims')
            else:
                dso[v].data = mavg_periodic(dset[v], n)

    return dso


def formatFloat(fmt, val):
    ret = fmt % val
    if ret.startswith("0."):
        return ret[1:]
    if ret.startswith("-0."):
        return "-" + ret[2:]
    return ret


def label_plots(fig, axs, xoff=-0.04, yoff=0.02):
    alp = [chr(i).upper() for i in range(97,97+26)]
    for i, ax in enumerate(axs):    
        p = ax.get_position()
        x = p.x0 + xoff
        y = p.y1 + yoff
        fig.text(x, y , f'{alp[i]}',
                 fontsize=14,
                 fontweight='semibold')    
        
        
# Ref: https://stackoverflow.com/questions/3173320/text-progress-bar-in-the-console
# https://gist.github.com/aubricus/f91fb55dc6ba5557fbab06119420dd6a
def print_progressbar(iteration, total, prefix='', suffix='', decimals=1, bar_length=100):
    """
    Call in a loop to create terminal progress bar
    @params:
        iteration   - Required  : current iteration (Int)
        total       - Required  : total iterations (Int)
        prefix      - Optional  : prefix string (Str)
        suffix      - Optional  : suffix string (Str)
        decimals    - Optional  : positive number of decimals in percent complete (Int)
        bar_length  - Optional  : character length of bar (Int)
    """
    str_format = '{0:.' + str(decimals) + 'f}'
    percents = str_format.format(100 * (iteration / float(total)))
    filled_length = int(round(bar_length * iteration / float(total)))
    bar = '█' * filled_length + '-' * (bar_length - filled_length)

    #print('\r%s |%s| %s%% %s' % (prefix, bar, percents, suffix), end='\r')
    #print('\r%s |%s| %s%s %s' % (prefix, bar, percents, '%', suffix))
    sys.stdout.write('%s |%s| %s%s %s\r' % (prefix, bar, percents, '%', suffix)),

    if iteration == total:
        print()
    sys.stdout.flush()        
    

def get_ftp_md5(ftp, remote_file):
    """Compute checksum on remote ftp file."""
    m = hashlib.md5()
    ftp.retrbinary(f'RETR {remote_file}', m.update)
    return m.hexdigest()


def get_loc_md5(local_path):
    """Compute checksum on local file."""
    with open(local_path, 'rb') as fid:
        data = fid.read()    
    return hashlib.md5(data).hexdigest()


def doy_midmonth():
    eomday = np.array([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]).cumsum()
    bomday = np.array([1, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30]).cumsum()
    return np.vstack((eomday, bomday)).mean(axis=0)    

def eomday(year, month):
    """end of month day"""
    if isinstance(year, Iterable):
        assert isinstance(month, Iterable)
        return np.array([calendar.monthrange(y, m)[-1] for y, m in zip(year, month)])
    else:
        return calendar.monthrange(year, month)[-1]


def nday_per_year(year):
    """number of days in a year"""
    if eomday(year, 2) == 29:
        return 366
    else:
        return 365


def to_datenum(y, m, d, time_units='days since 0001-01-01 00:00:00'):
    """convert year, month, day to number"""      
    return cftime.date2num(cftime.datetime(y, m, d), units=time_units)


def year_frac(year, month, day):
    """compute year fraction"""
    if isinstance(year, Iterable):
        assert isinstance(month, Iterable)
        assert isinstance(day, Iterable)
        t0_year = np.array([to_datenum(y, 1, 1) - 1 for y in year])
        t_year = np.array([to_datenum(y, m, d) for y, m, d in zip(year, month, day)])
        nday_year = np.array([nday_per_year(y) for y in year])
    else:
        t0_year = to_datenum(year, 1, 1) - 1
        t_year = to_datenum(year, month, day)
        nday_year = nday_per_year(year)
    
    return year + (t_year - t0_year) / nday_year


def ann_mean(ds, season=None, time_bnds_varname='time_bnds', time_centered=True, n_req=None):
    """Compute annual means, or optionally seasonal means"""
    
    ds = ds.copy() #deep=True)

    if n_req is None:
        if season is not None:
            n_req = 2
        else:
            n_req = 8
    
    if time_bnds_varname is None and not time_centered:
        raise NotImplementedError('time_bnds_varname cannot be "None" if time_centered=False')
        
    if not time_centered:
        time_units = ds.time.encoding['units']
        time_calendar = ds.time.encoding['calendar']

        # compute time bounds array
        time_bound_data = cftime.date2num(
                ds[time_bnds_varname].data, 
                units=time_units, 
                calendar=time_calendar)    

        # center time
        time_centered = cftime.num2date(
            time_bound_data.mean(axis=1),
            units=time_units, 
            calendar=time_calendar
        )        
        time_attrs = ds.time.attrs
        time_encoding = ds.time.encoding

        ds['time'] = xr.DataArray(
            time_centered,
            dims=('time')
        )    
    
    ones = xr.DataArray(
        np.ones((len(ds.time))), 
        dims=('time'), 
        coords={'time': ds.time},
    )
    time_mask = xr.DataArray(
        np.ones((len(ds.time))), 
        dims=('time'), 
        coords={'time': ds.time},
    )

    group_by_year = 'time.year'
    rename = {'year': 'time'}
    
    if season is not None:
        season = season.upper()
        if season not in ['DJF', 'MAM', 'JJA', 'SON']:
            raise ValueError(f'unknown season: {season}')            

        ds['austral_year'] = xr.where(ds['time.month'] > 6, ds['time.year'] + 1, ds['time.year'])
        ds = ds.set_coords('austral_year')
        ones = ones.assign_coords({'austral_year': ds.austral_year})
        time_mask = time_mask.assign_coords({'austral_year': ds.austral_year})
        time_mask = time_mask.where(ds['time.season'] == season).fillna(0)
        
        if season == 'DJF':
            group_by_year = 'austral_year'
            rename = {'austral_year': 'time'}
            
    if time_bnds_varname is not None:
        time_wgt = ds[time_bnds_varname].diff(dim=ds[time_bnds_varname].dims[1])
        if time_wgt.dtype == '<m8[ns]':
            time_wgt = time_wgt / np.timedelta64(1, 'D')
    else:        
        time_wgt = xr.DataArray(
            np.ones((len(ds.time))), 
            dims=('time'), 
            coords={'time': ds.time},
        )
        time_wgt = time_wgt.assign_coords(
            {c: da for c, da in ds.coords.items() if 'time' in da.dims}
        )
                       
    time_wgt = time_wgt.where(time_mask==1) #.fillna(0.)

    ones = ones.where(time_mask==1)
    time_wgt_grouped = time_wgt.groupby(group_by_year, restore_coord_dims=False)
    time_wgt = time_wgt_grouped / time_wgt_grouped.sum(dim=xr.ALL_DIMS)

    nyr = len(time_wgt_grouped.groups)
         
    time_wgt = time_wgt.squeeze()

    idx_not_nans = ~np.isnan(time_wgt)
    sum_wgt = time_wgt.groupby(group_by_year).sum(dim=xr.ALL_DIMS)
    idx_not_nans = (sum_wgt > 0)

    np.testing.assert_almost_equal(
        sum_wgt[idx_not_nans], 
        np.ones(idx_not_nans.sum().values)
    )

    nontime_vars = set([v for v in ds.variables if 'time' not in ds[v].dims]) - set(ds.coords)
    dsop = ds.drop_vars(nontime_vars)

    if time_bnds_varname is not None:
        dsop = dsop.drop_vars(time_bnds_varname)    
    
    def weighted_mean_arr(darr, wgts=None):
        # if NaN are present, we need to use individual weights
        cond = darr.isnull()
        ones = xr.where(cond, 0.0, 1.0)
        if season is None:
            mask = (
                darr.resample({'time': 'A'}, restore_coord_dims=False).mean(dim='time').notnull()
            )
            da_sum = (
                (darr * wgts).resample({'time': 'A'}, restore_coord_dims=False).sum(dim='time')
            )
            ones_out = (
                (ones * wgts).resample({'time': 'A'}, restore_coord_dims=False).sum(dim='time')
            )
            count = (
                (ones * wgts.notnull()).resample({'time': 'A'}, restore_coord_dims=False).sum(dim='time')
            )
        else:
            mask = (
                darr.groupby(group_by_year, restore_coord_dims=False).mean(dim='time').notnull()
            ).rename(rename)
            
            da_sum = (
                (darr * wgts).groupby(group_by_year, restore_coord_dims=False).sum(dim='time')
            ).rename(rename)
            
            ones_out = (
                (ones * wgts).groupby(group_by_year, restore_coord_dims=False).sum(dim='time')
            ).rename(rename)
            
            count = (
                 (ones * wgts.notnull()).groupby(group_by_year, restore_coord_dims=False).sum(dim='time')
            ).rename(rename)

        ones_out = ones_out.where(ones_out > 0.0)
        da_weighted_mean = da_sum / ones_out

        return da_weighted_mean.where(mask).where(count >= n_req)    

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        ds_ann = dsop.map(weighted_mean_arr, wgts=time_wgt)

    # copy attrs
    for v in ds_ann:
        ds_ann[v].attrs = ds[v].attrs

    # restore coords
    ds_ann = xr.merge((ds_ann, ds[list(nontime_vars)]))

    # eliminate partials
    ndx = (time_wgt_grouped.count(dim=xr.ALL_DIMS) >= n_req).values
    if not ndx.all():
        ds_ann = ds_ann.isel(time=ndx)

    return ds_ann


def np_datetime64_to_year(dt):
    return dt.time.values.astype('datetime64[Y]').astype(int) + 1970


def label_stations(ax, stninfo, fontsize=10, color='#e41a1c'):
    
    with open('data/marker_xy_offsets_stations.yaml', 'r') as fid:
        stn_marker = yaml.safe_load(fid)
    
    
    plotted = []
    for stn_inst_type in stninfo.index:
        stn = stninfo.loc[stn_inst_type].stn

        if stn in plotted:
            continue                   
        slon = stninfo.loc[stn_inst_type].lon
        slat = stninfo.loc[stn_inst_type].lat
        plotted.append(stn)

        ax.plot(slon, slat, '*', 
                markersize=12, 
                transform=ccrs.PlateCarree(), 
                color=color)
        
        if stn in stn_marker:
            xo = stn_marker[stn]['lon']
            yo = stn_marker[stn]['lat']
            horizalign = 'left'
            if 'horizontalalignment' in stn_marker[stn]:
                ha = stn_marker[stn]['horizontalalignment']
        else:
            xo = 1
            yo = 1
            
        t = ax.text(slon+xo, slat+yo, stn, 
                    fontsize=fontsize,
                    ha=ha,
                    transform=ccrs.PlateCarree(), 
                    color=color)
    


def canvas(*args, figsize=(6, 4), use_gridspec=False, **gridspec_kwargs):

    assert len(args), 'Args required'
    assert len(args) <= 2, 'Too many args'
    
    if len(args) == 2:
        nrow = args[0]
        ncol = args[1]
    else:
        npanel = args[0]
        nrow = int(np.sqrt(npanel))
        ncol = int(npanel/nrow) + min(1, npanel%nrow)
    
    if use_gridspec:
        fig = plt.figure(figsize=(figsize[0]*ncol, figsize[1]*nrow)) #dpi=300)
        gs = gridspec.GridSpec(nrows=nrow, ncols=ncol, **gridspec_kwargs)
        axs = np.empty((nrow, ncol)).astype(object)
        for i, j in product(range(nrow), range(ncol)):
            axs[i, j] = plt.subplot(gs[i, j])
        return fig, axs
    else:
        return plt.subplots(
            nrow, ncol, 
            figsize=(figsize[0]*ncol, figsize[1]*nrow),                       
            constrained_layout=False,
            squeeze=False,
        )       


def datetime64_parts(da_time):
    year = da_time.values.astype('datetime64[Y]').astype(int) + 1970
    month = (da_time.values.astype('datetime64[M]').astype(int) % 12 + 1).astype(int)    
    day = (da_time.values.astype('datetime64[D]') - da_time.values.astype('datetime64[M]') + 1).astype(int)
    return year, month, day

def datetime64_parts_arr(da_time):
    year = da_time.astype('datetime64[Y]').astype(int) + 1970
    month = (da_time.astype('datetime64[M]').astype(int) % 12 + 1).astype(int)    
    day = (da_time.astype('datetime64[D]') - da_time.astype('datetime64[M]') + 1).astype(int)
    return year, month, day


def list_set(seq):
    """return a list with unique entries, but don't change the order"""
    seen = set()
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]


def subplot_col_labels(axs, col_labels, xoff=0.):
    assert len(axs) == len(col_labels)
    for ax, col_label in zip(axs, col_labels):
        ax.annotate(col_label,
                    xy=(np.mean(ax.get_xlim())+xoff, np.max(ax.get_ylim()) - np.diff(ax.get_ylim())*0.12), 
                    xytext=(np.mean(ax.get_xlim())+xoff, np.max(ax.get_ylim()) + np.diff(ax.get_ylim())*0.12), 
                    fontsize='14', fontweight='bold', ha='center', va='center')

def subplot_row_labels(axs, row_labels, yoff=0., xoff=0.):    
    assert len(axs) == len(row_labels)
    for ax, row_label in zip(axs, row_labels):
        ax.annotate(row_label, xy=(0+xoff, 0.5+yoff), xytext=(-ax.yaxis.labelpad-12+xoff, 0+yoff),
                    xycoords=ax.yaxis.label, textcoords='offset points',
                    rotation=90,
                    fontsize='14', fontweight='bold', ha='center', va='center')    
        
        
class timer(object):
    def __init__(self, name=None, normalize=None):
        self.name = name
        self.normalize = normalize
        if normalize:
            self.norm_str = '/process'
        else:
            self.norm_str = ''
    def __enter__(self):
        self.tstart = time.time()
    def __exit__(self, type, value, traceback):
        if self.name:
            print(f'[{self.name}]: ', end='')
        tic_toc = time.time() - self.tstart
        if self.normalize:
            tic_toc = tic_toc/self.normalize            
        print(f'{tic_toc:0.5f}s{self.norm_str}')     

        
def nanmedian(x):
    if np.isnan(x).all():
        return np.nan
    else:
        return np.nanmedian(x)        
    
    
def pd_xs_list(df, ndx_list, level):
    """take cross-section of data frame based on list of index values"""
    df_list = []
    for ndx in ndx_list:
        dfi = df.xs(ndx, level=level, drop_level=False).copy()
        #for k, v in zip(level, ndx):
        #    dfi[k] = v
        df_list.append(dfi)
    return pd.concat(df_list).reset_index()