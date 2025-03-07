"""
Functionality for reading ICEYE complex data into a SICD model.
"""

__classification__ = "UNCLASSIFIED"
__author__ = "Thomas McCullough"


import logging
import os
from typing import Union, Tuple, Sequence, Optional

import numpy
from numpy.polynomial import polynomial
from scipy.constants import speed_of_light

from sarpy.io.complex.nisar import _stringify
from sarpy.io.complex.base import SICDTypeReader
from sarpy.io.complex.sicd_elements.blocks import Poly2DType, Poly1DType
from sarpy.io.complex.sicd_elements.SICD import SICDType
from sarpy.io.complex.sicd_elements.CollectionInfo import CollectionInfoType, \
    RadarModeType
from sarpy.io.complex.sicd_elements.ImageCreation import ImageCreationType
from sarpy.io.complex.sicd_elements.RadarCollection import RadarCollectionType, \
    ChanParametersType, WaveformParametersType
from sarpy.io.complex.sicd_elements.ImageData import ImageDataType
from sarpy.io.complex.sicd_elements.GeoData import GeoDataType, SCPType
from sarpy.io.complex.sicd_elements.Position import PositionType, XYZPolyType
from sarpy.io.complex.sicd_elements.Grid import GridType, DirParamType, WgtTypeType
from sarpy.io.complex.sicd_elements.Timeline import TimelineType, IPPSetType
from sarpy.io.complex.sicd_elements.ImageFormation import ImageFormationType, \
    RcvChanProcType
from sarpy.io.complex.sicd_elements.RMA import RMAType, INCAType
from sarpy.io.complex.sicd_elements.Radiometric import RadiometricType
from sarpy.io.complex.utils import fit_position_xvalidation, two_dim_poly_fit

from sarpy.io.general.base import SarpyIOError
from sarpy.io.general.data_segment import HDF5DatasetSegment, BandAggregateSegment
from sarpy.io.general.format_function import ComplexFormatFunction
from sarpy.io.general.utils import get_seconds, parse_timestring, is_file_like, is_hdf5, h5py

logger = logging.getLogger(__name__)


def _parse_time(input_str: Union[bytes, str]) -> numpy.datetime64:
    """
    Parse the timestring.

    Parameters
    ----------
    input_str : bytes|str

    Returns
    -------
    numpy.datetime64
    """

    return parse_timestring(_stringify(input_str), precision='us')


class ICEYEDetails(object):
    """
    Parses and converts the ICEYE metadata.
    """
    __slots__ = ('_file_name', )

    def __init__(self, file_name: str):
        """

        Parameters
        ----------
        file_name : str
        """

        if h5py is None:
            raise ImportError("Can't read ICEYE files, because the h5py dependency is missing.")

        if not os.path.isfile(file_name):
            raise SarpyIOError('Path {} is not a file'.format(file_name))

        with h5py.File(file_name, 'r') as hf:
            if 's_q' not in hf or 's_i' not in hf:
                raise SarpyIOError(
                    'The hdf file does not have the real (s_q) or imaginary dataset (s_i).')
            if 'satellite_name' not in hf:
                raise SarpyIOError('The hdf file does not have the satellite_name dataset.')
            if 'product_name' not in hf:
                raise SarpyIOError('The hdf file does not have the product_name dataset.')

        self._file_name = file_name

    @property
    def file_name(self) -> str:
        """
        str: the file name
        """

        return self._file_name

    def get_sicd(self) -> (SICDType, Optional[Tuple[int, ...]], Tuple[int, ...]):
        """
        Gets the SICD structure and associated details for constructing the data segment.

        Returns
        -------
        sicd : SICDType
        reverse_axes : None|Tuple[int, ...]
        transpose_axes : Tuple[int, ...]
        """

        def get_collection_info() -> CollectionInfoType:

            mode_id_type_map = {
                "SpotlightExtendedArea": "DYNAMIC STRIPMAP", }
            mode_id = _stringify(hf['product_type'][()])
            mode_type = mode_id_type_map.get(mode_id, _stringify(hf['acquisition_mode'][()]).upper())

            return CollectionInfoType(
                CollectorName=_stringify(hf['satellite_name'][()]),
                CoreName=_stringify(hf['product_name'][()]),
                CollectType='MONOSTATIC',
                Classification='UNCLASSIFIED',
                RadarMode=RadarModeType(
                    ModeType=mode_type,
                    ModeID=mode_id))

        def get_image_creation() -> ImageCreationType:
            from sarpy.__about__ import __version__
            return ImageCreationType(
                Application='ICEYE_P_{}'.format(hf['processor_version'][()]),
                DateTime=_parse_time(hf['processing_time'][()]),
                Site='Unknown',
                Profile='sarpy {}'.format(__version__))

        def get_image_data() -> ImageDataType:

            samp_prec = _stringify(hf['sample_precision'][()])
            if samp_prec.upper() == 'INT16':
                pixel_type = 'RE16I_IM16I'
            elif samp_prec.upper() == 'FLOAT32':
                pixel_type = 'RE32F_IM32F'
            else:
                raise ValueError('Got unhandled sample precision {}'.format(samp_prec))

            num_rows = int(number_of_range_samples)
            num_cols = int(number_of_azimuth_samples)
            scp_row = int(coord_center[0]) - 1
            scp_col = int(coord_center[1]) - 1
            if 0 < scp_col < num_rows-1:
                if look_side == 'left':
                    scp_col = num_cols - scp_col - 1
            else:
                # early ICEYE processing bug led to nonsensical SCP
                scp_col = int(num_cols/2.0)

            return ImageDataType(
                PixelType=pixel_type,
                NumRows=num_rows,
                NumCols=num_cols,
                FirstRow=0,
                FirstCol=0,
                FullImage=(num_rows, num_cols),
                SCPPixel=(scp_row, scp_col))

        def get_geo_data() -> GeoDataType:
            # NB: the remainder will be derived.
            return GeoDataType(
                SCP=SCPType(
                    LLH=[coord_center[2], coord_center[3], avg_scene_height]))

        def get_timeline() -> TimelineType:
            acq_prf = hf['acquisition_prf'][()]
            return TimelineType(
                CollectStart=start_time,
                CollectDuration=duration,
                IPP=[IPPSetType(index=0, TStart=0, TEnd=duration,
                                IPPStart=0, IPPEnd=int(round(acq_prf*duration)),
                                IPPPoly=[0, acq_prf]), ])

        def get_position() -> PositionType:
            times_str = hf['state_vector_time_utc'][:, 0]
            times = numpy.zeros((times_str.shape[0], ), dtype='float64')
            positions = numpy.zeros((times.size, 3), dtype='float64')
            velocities = numpy.zeros((times.size, 3), dtype='float64')
            for i, entry in enumerate(times_str):
                times[i] = get_seconds(_parse_time(entry), start_time, precision='us')
            positions[:, 0], positions[:, 1], positions[:, 2] = hf['posX'][:], hf['posY'][:], hf['posZ'][:]
            velocities[:, 0], velocities[:, 1], velocities[:, 2] = hf['velX'][:], hf['velY'][:], hf['velZ'][:]
            # fir the the position polynomial using cross validation
            P_x, P_y, P_z = fit_position_xvalidation(times, positions, velocities, max_degree=8)
            return PositionType(ARPPoly=XYZPolyType(X=P_x, Y=P_y, Z=P_z))

        def get_radar_collection() -> RadarCollectionType:
            return RadarCollectionType(
                TxPolarization=tx_pol,
                TxFrequency=(min_freq, max_freq),
                Waveform=[WaveformParametersType(TxFreqStart=min_freq,
                                                 TxRFBandwidth=tx_bandwidth,
                                                 TxPulseLength=hf['chirp_duration'][()],
                                                 ADCSampleRate=hf['range_sampling_rate'][()],
                                                 RcvDemodType='CHIRP',
                                                 RcvFMRate=0,
                                                 index=1)],
                RcvChannels=[ChanParametersType(TxRcvPolarization=polarization,
                                                index=1)])

        def get_image_formation() -> ImageFormationType:
            return ImageFormationType(
                TxRcvPolarizationProc=polarization,
                ImageFormAlgo='RMA',
                TStartProc=0,
                TEndProc=duration,
                TxFrequencyProc=(min_freq, max_freq),
                STBeamComp='NO',
                ImageBeamComp='SV',
                AzAutofocus='NO',
                RgAutofocus='NO',
                RcvChanProc=RcvChanProcType(NumChanProc=1, PRFScaleFactor=1, ChanIndices=[1, ]),)

        def get_radiometric() -> RadiometricType:
            return RadiometricType(BetaZeroSFPoly=[[float(hf['calibration_factor'][()]), ],])

        def calculate_drate_sf_poly() -> (numpy.ndarray, numpy.ndarray):
            r_ca_coeffs = numpy.array([r_ca_scp, 1], dtype='float64')
            dop_rate_coeffs = hf['doppler_rate_coeffs'][:]
            # Prior to ICEYE 1.14 processor, absolute value of Doppler rate was
            # provided, not true Doppler rate. Doppler rate should always be negative
            if dop_rate_coeffs[0] > 0:
                dop_rate_coeffs *= -1
            dop_rate_poly = Poly1DType(Coefs=dop_rate_coeffs)
            # now adjust to create
            t_drate_ca_poly = dop_rate_poly.shift(
                t_0=zd_ref_time - rg_time_scp,
                alpha=2/speed_of_light, return_poly=False)
            return t_drate_ca_poly, -polynomial.polymul(t_drate_ca_poly, r_ca_coeffs)*speed_of_light/(2*center_freq*vm_ca_sq)

        def calculate_doppler_polys() -> (numpy.ndarray, numpy.ndarray):
            # extract doppler centroid coefficients
            dc_estimate_coeffs = hf['dc_estimate_coeffs'][:]
            dc_time_str = hf['dc_estimate_time_utc'][:, 0]
            dc_zd_times = numpy.zeros((dc_time_str.shape[0], ), dtype='float64')
            for i, entry in enumerate(dc_time_str):
                dc_zd_times[i] = get_seconds(_parse_time(entry), start_time, precision='us')
            # create a sampled doppler centroid
            samples = 49  # copied from corresponding matlab, we just need enough for appropriate refitting
            # create doppler time samples
            diff_time_rg = first_pixel_time - zd_ref_time + \
                           numpy.linspace(0, number_of_range_samples/range_sampling_rate, samples)
            # doppler centroid samples definition
            dc_sample_array = numpy.zeros((samples, dc_zd_times.size), dtype='float64')
            for i, coeffs in enumerate(dc_estimate_coeffs):
                dc_sample_array[:, i] = polynomial.polyval(diff_time_rg, coeffs)
            # create arrays for range/azimuth from scp in meters
            azimuth_scp_m, range_scp_m = numpy.meshgrid(
                col_ss*(dc_zd_times - zd_time_scp)/ss_zd_s,
                (diff_time_rg + zd_ref_time - rg_time_scp)*speed_of_light/2)

            # fit the doppler centroid sample array
            x_order = min(3, range_scp_m.shape[0]-1)
            y_order = min(3, range_scp_m.shape[1]-1)

            t_dop_centroid_poly, residuals, rank, sing_values = two_dim_poly_fit(
                range_scp_m, azimuth_scp_m, dc_sample_array, x_order=x_order, y_order=y_order,
                x_scale=1e-3, y_scale=1e-3, rcond=1e-40)
            logger.info(
                'The dop_centroid_poly fit details:\n\troot mean square '
                'residuals = {}\n\trank = {}\n\tsingular values = {}'.format(
                    residuals, rank, sing_values))

            # define and fit the time coa array
            doppler_rate_sampled = polynomial.polyval(azimuth_scp_m, drate_ca_poly)
            time_coa = dc_zd_times + dc_sample_array/doppler_rate_sampled
            t_time_coa_poly, residuals, rank, sing_values = two_dim_poly_fit(
                range_scp_m, azimuth_scp_m, time_coa, x_order=x_order, y_order=y_order,
                x_scale=1e-3, y_scale=1e-3, rcond=1e-40)
            logger.info(
                'The time_coa_poly fit details:\n\troot mean square '
                'residuals = {}\n\trank = {}\n\tsingular values = {}'.format(
                    residuals, rank, sing_values))
            return t_dop_centroid_poly, t_time_coa_poly

        def get_rma() -> RMAType:
            dop_centroid_poly = Poly2DType(Coefs=dop_centroid_poly_coeffs)
            dop_centroid_coa = True
            if collect_info.RadarMode.ModeType == 'SPOTLIGHT':
                dop_centroid_poly = None
                dop_centroid_coa = None
            # NB: DRateSFPoly is defined as a function of only range - reshape appropriately
            inca = INCAType(
                R_CA_SCP=r_ca_scp,
                FreqZero=center_freq,
                DRateSFPoly=Poly2DType(Coefs=numpy.reshape(drate_sf_poly_coefs, (-1, 1))),
                DopCentroidPoly=dop_centroid_poly,
                DopCentroidCOA=dop_centroid_coa,
                TimeCAPoly=Poly1DType(Coefs=time_ca_poly_coeffs))
            return RMAType(
                RMAlgoType='OMEGA_K',
                INCA=inca)

        def get_grid() -> GridType:
            time_coa_poly = Poly2DType(Coefs=time_coa_poly_coeffs)
            if collect_info.RadarMode.ModeType == 'SPOTLIGHT':
                time_coa_poly = Poly2DType(Coefs=[[float(time_coa_poly_coeffs[0, 0]), ], ])

            row_win = _stringify(hf['window_function_range'][()])
            if row_win == 'NONE':
                row_win = 'UNIFORM'
            row = DirParamType(
                SS=row_ss,
                Sgn=-1,
                KCtr=2*center_freq/speed_of_light,
                ImpRespBW=2*tx_bandwidth/speed_of_light,
                DeltaKCOAPoly=Poly2DType(Coefs=[[0,]]),
                WgtType=WgtTypeType(WindowName=row_win))
            col_win = _stringify(hf['window_function_azimuth'][()])
            if col_win == 'NONE':
                col_win = 'UNIFORM'
            col = DirParamType(
                SS=col_ss,
                Sgn=-1,
                KCtr=0,
                ImpRespBW=col_imp_res_bw,
                WgtType=WgtTypeType(WindowName=col_win),
                DeltaKCOAPoly=Poly2DType(Coefs=dop_centroid_poly_coeffs*ss_zd_s/col_ss))
            return GridType(
                Type='RGZERO',
                ImagePlane='SLANT',
                TimeCOAPoly=time_coa_poly,
                Row=row,
                Col=col)

        def correct_scp() -> None:
            scp_pixel = sicd.ImageData.SCPPixel.get_array()
            scp_ecf = sicd.project_image_to_ground(scp_pixel, projection_type='HAE')
            sicd.update_scp(scp_ecf, coord_system='ECF')

        with h5py.File(self._file_name, 'r') as hf:
            # some common use variables
            look_side = _stringify(hf['look_side'][()])
            coord_center = hf['coord_center'][:]
            avg_scene_height = float(hf['avg_scene_height'][()])
            start_time = _parse_time(hf['acquisition_start_utc'][()])
            end_time = _parse_time(hf['acquisition_end_utc'][()])
            duration = get_seconds(end_time, start_time, precision='us')

            center_freq = float(hf['carrier_frequency'][()])
            tx_bandwidth = float(hf['chirp_bandwidth'][()])
            min_freq = center_freq-0.5*tx_bandwidth
            max_freq = center_freq+0.5*tx_bandwidth

            pol_temp = _stringify(hf['polarization'][()])
            tx_pol = pol_temp[0]
            rcv_pol = pol_temp[1]
            polarization = tx_pol + ':' + rcv_pol

            first_pixel_time = float(hf['first_pixel_time'][()])
            near_range = first_pixel_time*speed_of_light/2
            number_of_range_samples = float(hf['number_of_range_samples'][()])
            number_of_azimuth_samples = float(hf['number_of_azimuth_samples'][()])
            range_sampling_rate = float(hf['range_sampling_rate'][()])
            row_ss = speed_of_light/(2*range_sampling_rate)

            # define the sicd elements
            collect_info = get_collection_info()
            image_creation = get_image_creation()
            image_data = get_image_data()
            geo_data = get_geo_data()
            timeline = get_timeline()
            position = get_position()
            radar_collection = get_radar_collection()
            image_formation = get_image_formation()
            radiometric = get_radiometric()

            # calculate some zero doppler parameters
            ss_zd_s = float(hf['azimuth_time_interval'][()])
            if look_side == 'left':
                ss_zd_s *= -1
                zero_doppler_left = _parse_time(hf['zerodoppler_end_utc'][()])
            else:
                zero_doppler_left = _parse_time(hf['zerodoppler_start_utc'][()])
            dop_bw = hf['total_processed_bandwidth_azimuth'][()]
            zd_time_scp = get_seconds(zero_doppler_left, start_time, precision='us') + \
                          image_data.SCPPixel.Col*ss_zd_s
            zd_ref_time = first_pixel_time + number_of_range_samples/(2*range_sampling_rate)
            vel_scp = position.ARPPoly.derivative_eval(zd_time_scp, der_order=1)
            vm_ca_sq = numpy.sum(vel_scp*vel_scp)
            rg_time_scp = first_pixel_time + image_data.SCPPixel.Row/range_sampling_rate
            r_ca_scp = rg_time_scp*speed_of_light/2
            # calculate the doppler rate sf polynomial
            drate_ca_poly, drate_sf_poly_coefs = calculate_drate_sf_poly()

            # calculate some doppler dependent grid parameters
            col_ss = float(numpy.sqrt(vm_ca_sq)*abs(ss_zd_s)*drate_sf_poly_coefs[0])
            col_imp_res_bw = dop_bw*abs(ss_zd_s)/col_ss
            time_ca_poly_coeffs = [zd_time_scp, ss_zd_s/col_ss]

            # calculate the doppler polynomials
            dop_centroid_poly_coeffs, time_coa_poly_coeffs = calculate_doppler_polys()

            # finish definition of sicd elements
            rma = get_rma()
            grid = get_grid()
            sicd = SICDType(
                CollectionInfo=collect_info,
                ImageCreation=image_creation,
                ImageData=image_data,
                GeoData=geo_data,
                Timeline=timeline,
                Position=position,
                RadarCollection=radar_collection,
                ImageFormation=image_formation,
                Radiometric=radiometric,
                RMA=rma,
                Grid=grid)
        # adjust the scp location
        correct_scp()
        # derive sicd fields
        sicd.derive()
        # TODO: RNIIRS?

        reverse_axes = (0, ) if look_side == 'left' else None
        transpose_axes = (1, 0)
        # noinspection PyTypeChecker
        return sicd, reverse_axes, transpose_axes


def get_iceye_data_segment(file_name: str,
                           reverse_axes: Union[None, int, Sequence[int]],
                           transpose_axes: Union[None, Tuple[int, ...]],
                           real_group: str='s_i',
                           imaginary_grop: str='s_q') -> BandAggregateSegment:
    real_dataset = HDF5DatasetSegment(
        file_name, real_group, reverse_axes=reverse_axes, transpose_axes=transpose_axes, close_file=True)
    imag_dataset = HDF5DatasetSegment(
        file_name, imaginary_grop, reverse_axes=reverse_axes, transpose_axes=transpose_axes, close_file=True)
    return BandAggregateSegment(
        (real_dataset, imag_dataset), band_dimension=2,
        formatted_dtype='complex64', formatted_shape=real_dataset.formatted_shape,
        format_function=ComplexFormatFunction(real_dataset.formatted_dtype, order='IQ', band_dimension=-1))


class ICEYEReader(SICDTypeReader):
    """
    An ICEYE SLC reader implementation.

    **Changed in version 1.3.0** for reading changes.
    """

    __slots__ = ('_iceye_details', )

    def __init__(self, iceye_details):
        """

        Parameters
        ----------
        iceye_details : str|ICEYEDetails
            file name or ICEYEDetails object
        """

        if isinstance(iceye_details, str):
            iceye_details = ICEYEDetails(iceye_details)
        if not isinstance(iceye_details, ICEYEDetails):
            raise TypeError('The input argument for a ICEYEReader must be a '
                            'filename or ICEYEDetails object')
        self._iceye_details = iceye_details
        sicd, reverse_axes, transpose_axes = iceye_details.get_sicd()
        data_segment = get_iceye_data_segment(iceye_details.file_name, reverse_axes, transpose_axes)

        SICDTypeReader.__init__(self, data_segment, sicd, close_segments=True)
        self._check_sizes()

    @property
    def iceye_details(self) -> ICEYEDetails:
        """
        ICEYEDetails: The ICEYE details object.
        """

        return self._iceye_details

    @property
    def file_name(self):
        return self.iceye_details.file_name


########
# base expected functionality for a module with an implemented Reader

def is_a(file_name: str) -> Union[None, ICEYEReader]:
    """
    Tests whether a given file_name corresponds to a ICEYE file. Returns a reader instance, if so.

    Parameters
    ----------
    file_name : str|BinaryIO
        the file_name to check

    Returns
    -------
    None|ICEYEReader
        `ICEYEReader` instance if ICEYE file, `None` otherwise
    """

    if is_file_like(file_name):
        return None

    if not is_hdf5(file_name):
        return None

    if h5py is None:
        return None

    try:
        iceye_details = ICEYEDetails(file_name)
        logger.info('File {} is determined to be a ICEYE file.'.format(file_name))
        return ICEYEReader(iceye_details)
    except SarpyIOError:
        return None

