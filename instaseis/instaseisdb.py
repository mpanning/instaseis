#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Python library to extract seismograms from a set of wavefields generated by
AxiSEM.

:copyright:
    Martin van Driel (Martin@vanDriel.de), 2014
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2014
:license:
    GNU General Public License, Version 3
    (http://www.gnu.org/copyleft/gpl.html)
"""
from __future__ import absolute_import

import collections
import numpy as np
from obspy.core import Stream, Trace
from obspy.signal.util import nextpow2
import os
import warnings

from . import InstaseisError, InstaseisNotFoundError
from . import finite_elem_mapping
from . import mesh
from . import rotations
from . import sem_derivatives
from . import spectral_basis
from . import lanczos
from instaseis.source import Source, ForceSource


MeshCollection_bwd = collections.namedtuple("MeshCollection_bwd", ["px", "pz"])
MeshCollection_fwd = collections.namedtuple("MeshCollection_fwd", ["m1", "m2",
                                                                   "m3", "m4"])

DEFAULT_MU = 32e9


class InstaSeisDB(object):
    """
    A class to extract Seismograms from a set of wavefields generated by
    AxiSEM. Taking advantage of reciprocity of the Green's function, two
    simulations with single force sources (vertical and horizontal) to build a
    complete Database of Green's function in global 1D models. The spatial
    discretization equals the SEM basis functions of AxiSEM, resulting in high
    order spatial accuracy and short access times.
    """
    def __init__(self, db_path, buffer_size_in_mb=100, read_on_demand=True):
        """
        :param db_path: Path to the AxiSEM Database containing subdirectories
            PZ and/or PX each containing a order_output.nc4 file
        :type db_path: str
        :param buffer_size_in_mb: Strain is buffered to avoid unnecessary
            file access when sources are located in the same SEM element
        :type buffer_size_in_mb: int, optional
        :param read_on_demand: read several global fields on demand (faster
            initialization, default) or on initialization (slower
            initialization, faster in individual seismogram extraction,
            useful e.g. for finite sources)
        :type read_on_demand: bool, optional
        """
        self.db_path = db_path
        self.buffer_size_in_mb = buffer_size_in_mb
        self.read_on_demand = read_on_demand
        self._find_and_open_files()

    def _find_and_open_files(self):
        """
        Helper function walking the file tree below self.db_path and
        attempts to find the correct netCDF files.
        """
        found_files = []
        for root, dirs, filenames in os.walk(self.db_path, followlinks=True):
            # Limit depth of filetree traversal
            nested_levels = os.path.relpath(root, self.db_path).split(
                os.path.sep)
            if len(nested_levels) >= 4:
                del dirs[:]
            if "ordered_output.nc4" not in filenames:
                continue
            found_files.append(os.path.join(root, "ordered_output.nc4"))

        if len(found_files) == 0:
            raise InstaseisNotFoundError(
                "No suitable netCDF files found under '%s'" % self.db_path)
        elif len(found_files) not in [1, 2, 4]:
            raise InstaseisError(
                "1, 2 or 4 netCDF must be present in the folder structure. "
                "Found %i: \t%s" % (len(found_files),
                                    "\n\t".join(found_files)))

        # Parse to find the correct components.
        netcdf_files = collections.defaultdict(list)
        patterns = ["PX", "PZ", "MZZ", "MXX_P_MYY", "MXZ_MYZ", "MXY_MXX_M_MYY"]
        for filename in found_files:
            s = os.path.relpath(filename, self.db_path).split(os.path.sep)
            for p in patterns:
                if p in s:
                    netcdf_files[p].append(filename)

        # Assert at most one file per type.
        for key, files in netcdf_files.items():
            if len(files) != 1:
                raise InstaseisError(
                    "Found %i files for component %s:\n\t%s" % (
                        len(files), key, "\n\t".join(files)))
            netcdf_files[key] = files[0]

        # Two valid cases.
        if "PX" in netcdf_files or "PZ" in netcdf_files:
            self._parse_fs_meshes(netcdf_files)
        elif "MZZ" in netcdf_files or "MXX_P_MYY" in netcdf_files or \
                "MXZ_MYZ" in netcdf_files or "MXY_MXX_M_MYY" or netcdf_files:
            if sorted(netcdf_files.keys()) != sorted([
                    "MZZ", "MXX_P_MYY", "MXZ_MYZ", "MXY_MXX_M_MYY"]):
                raise InstaseisError(
                    "Expecting all four elemental moment tensor subfolders "
                    "to be present.")
            self._parse_mt_meshes(netcdf_files)
        else:
            raise InstaseisError("Could not find any suitable netCDF files.")

        # Set some common variables.
        self.nfft = nextpow2(self.ndumps) * 2
        self.planet_radius = self.parsed_mesh.planet_radius
        self.dump_type = self.parsed_mesh.dump_type

    def _parse_fs_meshes(self, files):
        if "PX" in files:
            px_file = files["PX"]
            x_exists = True
        else:
            x_exists = False
        if "PZ" in files:
            pz_file = files["PZ"]
            z_exists = True
        else:
            z_exists = False

        # full_parse will force the kd-tree to be built
        if x_exists and z_exists:
            px_m = mesh.Mesh(
                px_file, full_parse=True,
                strain_buffer_size_in_mb=self.buffer_size_in_mb,
                displ_buffer_size_in_mb=0,
                read_on_demand=self.read_on_demand)
            pz_m = mesh.Mesh(
                pz_file, full_parse=False,
                strain_buffer_size_in_mb=self.buffer_size_in_mb,
                displ_buffer_size_in_mb=0,
                read_on_demand=self.read_on_demand)
            self.parsed_mesh = px_m
        elif x_exists:
            px_m = mesh.Mesh(
                px_file, full_parse=True,
                strain_buffer_size_in_mb=self.buffer_size_in_mb,
                displ_buffer_size_in_mb=0,
                read_on_demand=self.read_on_demand)
            pz_m = None
            self.parsed_mesh = px_m
        elif z_exists:
            px_m = None
            pz_m = mesh.Mesh(
                pz_file, full_parse=True,
                strain_buffer_size_in_mb=self.buffer_size_in_mb,
                displ_buffer_size_in_mb=0,
                read_on_demand=self.read_on_demand)
            self.parsed_mesh = pz_m
        else:
            # Should not happen.
            raise NotImplementedError
        self.meshes = MeshCollection_bwd(px=px_m, pz=pz_m)
        self.reciprocal = True

    def _parse_mt_meshes(self, files):
        m1_m = mesh.Mesh(
            files["MZZ"], full_parse=True, strain_buffer_size_in_mb=0,
            displ_buffer_size_in_mb=self.buffer_size_in_mb,
            read_on_demand=self.read_on_demand)
        m2_m = mesh.Mesh(
            files["MXX_P_MYY"], full_parse=False, strain_buffer_size_in_mb=0,
            displ_buffer_size_in_mb=self.buffer_size_in_mb,
            read_on_demand=self.read_on_demand)
        m3_m = mesh.Mesh(
            files["MXZ_MYZ"], full_parse=False, strain_buffer_size_in_mb=0,
            displ_buffer_size_in_mb=self.buffer_size_in_mb,
            read_on_demand=self.read_on_demand)
        m4_m = mesh.Mesh(
            files["MXY_MXX_M_MYY"], full_parse=False,
            strain_buffer_size_in_mb=0,
            displ_buffer_size_in_mb=self.buffer_size_in_mb,
            read_on_demand=self.read_on_demand)
        self.parsed_mesh = m1_m

        self.meshes = MeshCollection_fwd(m1_m, m2_m, m3_m, m4_m)
        self.reciprocal = False

    def get_seismograms(self, source, receiver, components=("Z", "N", "E"),
                        remove_source_shift=True, reconvolve_stf=False,
                        return_obspy_stream=True, dt=None, a_lanczos=5):
        """
        Extract seismograms for a moment tensor point source from the AxiSEM
        database.

        :param source: instaseis.Source or instaseis.ForceSource object
        :type source: :class:`instaseis.source.Source` or
            :class:`instaseis.source.ForceSource`
        :param receiver: instaseis.Receiver object
        :type receiver: :class:`instaseis.source.Receiver`
        :param components: a tuple containing any combination of the
            strings ``"Z"``, ``"N"``, ``"E"``, ``"R"``, and ``"T"``
        :param remove_source_shift: move the starttime to the peak of the
            sliprate from the source time function used to generate the
            database
        :param reconvolve_stf: deconvolve the source time function used in
            the AxiSEM run and convolve with the stf attached to the source.
            For this to be stable, the new stf needs to bandlimited.
        :param return_obspy_stream: return format is either an obspy.Stream
            object or a plain array containing the data
        :param dt: desired sampling of the seismograms. resampling is done
            using a lanczos kernel
        :param a_lanczos: width of the kernel used in resampling
        """
        if self.reciprocal:
            if any(comp in components for comp in ['N', 'E', 'R', 'T']) and \
                    self.meshes.px is None:
                raise ValueError("vertical component only DB")

            if 'Z' in components and self.meshes.pz is None:
                raise ValueError("horizontal component only DB")

            if receiver.depth_in_m is not None:
                warnings.warn('Receiver depth cannot be changed when reading '
                              'from reciprocal DB. Using depth from the DB.')

            rotmesh_s, rotmesh_phi, rotmesh_z = rotations.rotate_frame_rd(
                source.x(planet_radius=self.planet_radius),
                source.y(planet_radius=self.planet_radius),
                source.z(planet_radius=self.planet_radius),
                receiver.longitude, receiver.colatitude)

        else:
            if source.depth_in_m is not None:
                warnings.warn('Source depth cannot be changed when reading '
                              'from forward DB. Using depth from the DB.')

            rotmesh_s, rotmesh_phi, rotmesh_z = rotations.rotate_frame_rd(
                receiver.x(planet_radius=self.planet_radius),
                receiver.y(planet_radius=self.planet_radius),
                receiver.z(planet_radius=self.planet_radius),
                source.longitude, source.colatitude)

        k_map = {"displ_only": 6,
                 "strain_only": 1,
                 "fullfields": 1}

        nextpoints = self.parsed_mesh.kdtree.query([rotmesh_s, rotmesh_z],
                                                   k=k_map[self.dump_type])

        # Find the element containing the point of interest.
        mesh = self.parsed_mesh.f.groups["Mesh"]
        if self.dump_type == 'displ_only':
            for idx in nextpoints[1]:
                corner_points = np.empty((4, 2), dtype="float64")

                if not self.read_on_demand:
                    corner_point_ids = self.parsed_mesh.fem_mesh[idx][:4]
                    eltype = self.parsed_mesh.eltypes[idx]
                    corner_points[:, 0] = \
                        self.parsed_mesh.mesh_S[corner_point_ids]
                    corner_points[:, 1] = \
                        self.parsed_mesh.mesh_Z[corner_point_ids]
                else:
                    corner_point_ids = mesh.variables["fem_mesh"][idx][:4]
                    eltype = mesh.variables["eltype"][idx]
                    corner_points[:, 0] = \
                        mesh.variables["mesh_S"][corner_point_ids]
                    corner_points[:, 1] = \
                        mesh.variables["mesh_Z"][corner_point_ids]

                isin, xi, eta = finite_elem_mapping.inside_element(
                    rotmesh_s, rotmesh_z, corner_points, eltype,
                    tolerance=1E-3)
                if isin:
                    id_elem = idx
                    break
            else:
                raise ValueError("Element not found")

            if not self.read_on_demand:
                gll_point_ids = self.parsed_mesh.sem_mesh[id_elem]
                axis = bool(self.parsed_mesh.axis[id_elem])
            else:
                gll_point_ids = mesh.variables["sem_mesh"][id_elem]
                axis = bool(mesh.variables["axis"][id_elem])

            if axis:
                col_points_xi = self.parsed_mesh.glj_points
                col_points_eta = self.parsed_mesh.gll_points
            else:
                col_points_xi = self.parsed_mesh.gll_points
                col_points_eta = self.parsed_mesh.gll_points
        else:
            id_elem = nextpoints[1]

        data = {}

        if self.reciprocal:

            fac_1_map = {"N": np.cos,
                         "E": np.sin}
            fac_2_map = {"N": lambda x: - np.sin(x),
                         "E": np.cos}

            if isinstance(source, Source):
                if self.dump_type == 'displ_only':
                    if axis:
                        G = self.parsed_mesh.G2
                        GT = self.parsed_mesh.G1T
                    else:
                        G = self.parsed_mesh.G2
                        GT = self.parsed_mesh.G2T

                strain_x = None
                strain_z = None

                # Minor optimization: Only read if actually requested.
                if "Z" in components:
                    if self.dump_type == 'displ_only':
                        strain_z = self.__get_strain_interp(
                            self.meshes.pz, id_elem, gll_point_ids, G, GT,
                            col_points_xi, col_points_eta, corner_points,
                            eltype, axis, xi, eta)
                    elif (self.dump_type == 'fullfields'
                            or self.dump_type == 'strain_only'):
                        strain_z = self.__get_strain(self.meshes.pz, id_elem)

                if any(comp in components for comp in ['N', 'E', 'R', 'T']):
                    if self.dump_type == 'displ_only':
                        strain_x = self.__get_strain_interp(
                            self.meshes.px, id_elem, gll_point_ids, G, GT,
                            col_points_xi, col_points_eta, corner_points,
                            eltype, axis, xi, eta)
                    elif (self.dump_type == 'fullfields'
                            or self.dump_type == 'strain_only'):
                        strain_x = self.__get_strain(self.meshes.px, id_elem)

                mij = rotations\
                    .rotate_symm_tensor_voigt_xyz_src_to_xyz_earth(
                        source.tensor_voigt, np.deg2rad(source.longitude),
                        np.deg2rad(source.colatitude))
                mij = rotations\
                    .rotate_symm_tensor_voigt_xyz_earth_to_xyz_src(
                        mij, np.deg2rad(receiver.longitude),
                        np.deg2rad(receiver.colatitude))
                mij = rotations.rotate_symm_tensor_voigt_xyz_to_src(
                    mij, rotmesh_phi)
                mij /= self.parsed_mesh.amplitude

                if "Z" in components:
                    final = np.zeros(strain_z.shape[0], dtype="float64")
                    for i in xrange(3):
                        final += mij[i] * strain_z[:, i]
                    final += 2.0 * mij[4] * strain_z[:, 4]
                    data["Z"] = final

                if "R" in components:
                    final = np.zeros(strain_x.shape[0], dtype="float64")
                    final -= strain_x[:, 0] * mij[0] * 1.0
                    final -= strain_x[:, 1] * mij[1] * 1.0
                    final -= strain_x[:, 2] * mij[2] * 1.0
                    final -= strain_x[:, 4] * mij[4] * 2.0
                    data["R"] = final

                if "T" in components:
                    final = np.zeros(strain_x.shape[0], dtype="float64")
                    final += strain_x[:, 3] * mij[3] * 2.0
                    final += strain_x[:, 5] * mij[5] * 2.0
                    data["T"] = final

                for comp in ["E", "N"]:
                    if comp not in components:
                        continue

                    fac_1 = fac_1_map[comp](rotmesh_phi)
                    fac_2 = fac_2_map[comp](rotmesh_phi)

                    final = np.zeros(strain_x.shape[0], dtype="float64")
                    final += strain_x[:, 0] * mij[0] * 1.0 * fac_1
                    final += strain_x[:, 1] * mij[1] * 1.0 * fac_1
                    final += strain_x[:, 2] * mij[2] * 1.0 * fac_1
                    final += strain_x[:, 3] * mij[3] * 2.0 * fac_2
                    final += strain_x[:, 4] * mij[4] * 2.0 * fac_1
                    final += strain_x[:, 5] * mij[5] * 2.0 * fac_2
                    if comp == "N":
                        final *= -1.0
                    data[comp] = final

            elif isinstance(source, ForceSource):
                if self.dump_type != 'displ_only':
                    raise ValueError("Force sources only in displ_only mode")

                if "Z" in components:
                    displ_z = self.__get_displacement(self.meshes.pz, id_elem,
                                                      gll_point_ids,
                                                      col_points_xi,
                                                      col_points_eta, xi, eta)

                if any(comp in components for comp in ['N', 'E', 'R', 'T']):
                    displ_x = self.__get_displacement(self.meshes.px, id_elem,
                                                      gll_point_ids,
                                                      col_points_xi,
                                                      col_points_eta, xi, eta)

                force = rotations.rotate_vector_xyz_src_to_xyz_earth(
                    source.force_tpr, np.deg2rad(source.longitude),
                    np.deg2rad(source.colatitude))
                force = rotations.rotate_vector_xyz_earth_to_xyz_src(
                    force, np.deg2rad(receiver.longitude),
                    np.deg2rad(receiver.colatitude))
                force = rotations.rotate_vector_xyz_to_src(
                    force, rotmesh_phi)
                force /= self.parsed_mesh.amplitude

                if "Z" in components:
                    final = np.zeros(displ_z.shape[0], dtype="float64")
                    final += displ_z[:, 0] * force[0]
                    final += displ_z[:, 2] * force[2]
                    data["Z"] = final

                if "R" in components:
                    final = np.zeros(displ_x.shape[0], dtype="float64")
                    final += displ_x[:, 0] * force[0]
                    final += displ_x[:, 2] * force[2]
                    data["R"] = final

                if "T" in components:
                    final = np.zeros(displ_x.shape[0], dtype="float64")
                    final += displ_x[:, 1] * force[1]
                    data["T"] = final

                for comp in ["E", "N"]:
                    if comp not in components:
                        continue

                    fac_1 = fac_1_map[comp](rotmesh_phi)
                    fac_2 = fac_2_map[comp](rotmesh_phi)

                    final = np.zeros(displ_x.shape[0], dtype="float64")
                    final += displ_x[:, 0] * force[0] * fac_1
                    final += displ_x[:, 1] * force[1] * fac_2
                    final += displ_x[:, 2] * force[2] * fac_1
                    if comp == "N":
                        final *= -1.0
                    data[comp] = final

            else:
                raise NotImplementedError

        else:
            if not isinstance(source, Source):
                raise NotImplementedError
            if self.dump_type != 'displ_only':
                raise NotImplementedError

            displ_1 = self.__get_displacement(self.meshes.m1, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)
            displ_2 = self.__get_displacement(self.meshes.m2, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)
            displ_3 = self.__get_displacement(self.meshes.m3, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)
            displ_4 = self.__get_displacement(self.meshes.m4, id_elem,
                                              gll_point_ids, col_points_xi,
                                              col_points_eta, xi, eta)

            mij = source.tensor / self.parsed_mesh.amplitude
            # mij is [m_rr, m_tt, m_pp, m_rt, m_rp, m_tp]
            # final is in s, phi, z coordinates
            final = np.zeros((displ_1.shape[0], 3), dtype="float64")

            final[:, 0] += displ_1[:, 0] * mij[0]
            final[:, 2] += displ_1[:, 2] * mij[0]

            final[:, 0] += displ_2[:, 0] * (mij[1] + mij[2])
            final[:, 2] += displ_2[:, 2] * (mij[1] + mij[2])

            fac_1 = mij[3] * np.cos(rotmesh_phi) \
                + mij[4] * np.sin(rotmesh_phi)
            fac_2 = -mij[3] * np.sin(rotmesh_phi) \
                + mij[4] * np.cos(rotmesh_phi)

            final[:, 0] += displ_3[:, 0] * fac_1
            final[:, 1] += displ_3[:, 1] * fac_2
            final[:, 2] += displ_3[:, 2] * fac_1

            fac_1 = (mij[1] - mij[2]) * np.cos(2 * rotmesh_phi) \
                + 2 * mij[5] * np.sin(2 * rotmesh_phi)
            fac_2 = -(mij[1] - mij[2]) * np.sin(2 * rotmesh_phi) \
                + 2 * mij[5] * np.cos(2 * rotmesh_phi)

            final[:, 0] += displ_4[:, 0] * fac_1
            final[:, 1] += displ_4[:, 1] * fac_2
            final[:, 2] += displ_4[:, 2] * fac_1

            rotmesh_colat = np.arctan2(rotmesh_s, rotmesh_z)

            if "T" in components:
                # need the - for consistency with reciprocal mode,
                # need external verification still
                data["T"] = -final[:, 1]

            if "R" in components:
                data["R"] = final[:, 0] * np.cos(rotmesh_colat) \
                    - final[:, 2] * np.sin(rotmesh_colat)

            if "N" in components or "E" in components or "Z" in components:
                # transpose needed because rotations assume different slicing
                # (ugly)
                final = rotations.rotate_vector_src_to_NEZ(
                    final.T, rotmesh_phi,
                    source.longitude_rad, source.colatitude_rad,
                    receiver.longitude_rad, receiver.colatitude_rad).T

                if "N" in components:
                    data["N"] = final[:, 0]
                if "E" in components:
                    data["E"] = final[:, 1]
                if "Z" in components:
                    data["Z"] = final[:, 2]

        for comp in components:
            if remove_source_shift and not reconvolve_stf:
                data[comp] = data[comp][self.parsed_mesh.source_shift_samp:]
            elif reconvolve_stf:
                if source.dt is None or source.sliprate is None:
                    raise RuntimeError("source has no source time function")

                stf_deconv_f = np.fft.rfft(
                    self.sliprate, n=self.nfft)

                if abs((source.dt - self.dt) / self.dt) > 1e-7:
                    raise ValueError("dt of the source not compatible")

                stf_conv_f = np.fft.rfft(source.sliprate,
                                         n=self.nfft)

                if source.time_shift is not None:
                    stf_conv_f *= \
                        np.exp(- 1j * np.fft.rfftfreq(self.nfft)
                               * 2. * np.pi * source.time_shift / self.dt)

                # TODO: double check wether a taper is needed at the end of the
                #       trace
                dataf = np.fft.rfft(data[comp], n=self.nfft)

                data[comp] = np.fft.irfft(
                    dataf * stf_conv_f / stf_deconv_f)[:self.ndumps]

            if dt is not None:
                data[comp] = lanczos.lanczos_resamp(
                    data[comp], self.parsed_mesh.dt, dt, a_lanczos)

        if return_obspy_stream:
            # Convert to an ObsPy Stream object.
            st = Stream()
            if dt is None:
                dt = self.parsed_mesh.dt
            band_code = self._get_band_code(dt)
            for comp in components:
                tr = Trace(data=data[comp],
                           header={"delta": dt,
                                   "station": receiver.station,
                                   "network": receiver.network,
                                   "channel": "%sX%s" % (band_code, comp)})
                st += tr
            return st
        else:
            npol = self.parsed_mesh.npol
            if not self.read_on_demand:
                mu = self.parsed_mesh.mesh_mu[gll_point_ids[npol/2, npol/2]]
            else:
                mu = mesh.variables["mesh_mu"][gll_point_ids[npol/2, npol/2]]
            return data, mu

    def get_seismograms_finite_source(self, sources, receiver,
                                      components=("Z", "N", "E"), dt=None,
                                      a_lanczos=5, correct_mu=False,
                                      progress_callback=None):
        """
        Extract seismograms for a finite source from the AxiSEM database
        provided as a list of point sources attached with source time functions
        and time shifts.

        :param sources: A collection of point sources.
        :type sources: list of :class:`instaseis.source.Source` objects
        :param receiver: The receiver location.
        :type receiver: :class:`instaseis.source.Receiver`
        :param components: a tuple containing any combination of the strings
            ``"Z"``, ``"N"``, and ``"E"``
        :param dt: desired sampling of the seismograms.resampling is done
            using a lanczos kernel
        :param a_lanczos: width of the kernel used in resampling
        :param correct_mu: correct the source magnitude for the actual shear
            modulus from the model
        """
        if not self.reciprocal:
            raise NotImplementedError

        data_summed = {}
        count = len(sources)
        for _i, source in enumerate(sources):
            data, mu = self.get_seismograms(
                source, receiver, components, reconvolve_stf=True,
                return_obspy_stream=False)

            if correct_mu:
                corr_fac = mu / DEFAULT_MU,
            else:
                corr_fac = 1

            for comp in components:
                if comp in data_summed:
                    data_summed[comp] += data[comp] * corr_fac
                else:
                    data_summed[comp] = data[comp] * corr_fac
            if progress_callback:
                cancel = progress_callback(_i + 1, count)
                if cancel:
                    return None

        if dt is not None:
            for comp in components:
                data_summed[comp] = lanczos.lanczos_resamp(
                    data_summed[comp], self.parsed_mesh.dt, dt, a_lanczos)

        # Convert to an ObsPy Stream object.
        st = Stream()
        if dt is None:
            dt = self.parsed_mesh.dt
        band_code = self._get_band_code(dt)
        for comp in components:
            tr = Trace(data=data_summed[comp],
                       header={"delta": dt,
                               "station": receiver.station,
                               "network": receiver.network,
                               "channel": "%sX%s" % (band_code, comp)})
            st += tr
        return st

    @staticmethod
    def _get_band_code(dt):
        """
        Figure out the channel band code. Done as in SPECFEM.
        """
        sr = 1.0 / dt
        if sr <= 0.001:
            band_code = "F"
        elif sr <= 0.004:
            band_code = "C"
        elif sr <= 0.0125:
            band_code = "H"
        elif sr <= 0.1:
            band_code = "B"
        elif sr < 1:
            band_code = "M"
        else:
            band_code = "L"
        return band_code

    def __get_strain_interp(self, mesh, id_elem, gll_point_ids, G, GT,
                            col_points_xi, col_points_eta, corner_points,
                            eltype, axis, xi, eta):
        if id_elem not in mesh.strain_buffer:
            # Single precision in the NetCDF files but the later interpolation
            # routines require double precision. Assignment to this array will
            # force a cast.
            utemp = np.zeros((mesh.ndumps, mesh.npol + 1, mesh.npol + 1, 3),
                             dtype=np.float64, order="F")

            mesh_dict = mesh.f.groups["Snapshots"].variables

            # Load displacement from all GLL points.
            for i, var in enumerate(["disp_s", "disp_p", "disp_z"]):
                if var not in mesh_dict:
                    continue
                temp = mesh_dict[var][:, gll_point_ids.flatten()]
                for ipol in xrange(mesh.npol + 1):
                    for jpol in xrange(mesh.npol + 1):
                        utemp[:, jpol, ipol, i] = temp[:, ipol * 5 + jpol]

            strain_fct_map = {
                "monopole": sem_derivatives.strain_monopole_td,
                "dipole": sem_derivatives.strain_dipole_td,
                "quadpole": sem_derivatives.strain_quadpole_td}

            strain = strain_fct_map[mesh.excitation_type](
                utemp, G, GT, col_points_xi, col_points_eta, mesh.npol,
                mesh.ndumps, corner_points, eltype, axis)

            mesh.strain_buffer.add(id_elem, strain)
        else:
            strain = mesh.strain_buffer.get(id_elem)

        final_strain = np.empty((strain.shape[0], 6), order="F")

        for i in xrange(6):
            final_strain[:, i] = spectral_basis.lagrange_interpol_2D_td(
                col_points_xi, col_points_eta, strain[:, :, :, i], xi, eta)

        if not mesh.excitation_type == "monopole":
            final_strain[:, 3] *= -1.0
            final_strain[:, 5] *= -1.0

        return final_strain

    def __get_strain(self, mesh, id_elem):
        if id_elem not in mesh.strain_buffer:
            strain_temp = np.zeros((self.ndumps, 6), order="F")

            mesh_dict = mesh.f.groups["Snapshots"].variables

            for i, var in enumerate([
                    'strain_dsus', 'strain_dsuz', 'strain_dpup',
                    'strain_dsup', 'strain_dzup', 'straintrace']):
                if var not in mesh_dict:
                    continue
                strain_temp[:, i] = mesh_dict[var][:, id_elem]

            # transform strain to voigt mapping
            # dsus, dpup, dzuz, dzup, dsuz, dsup
            final_strain = np.empty((self.ndumps, 6), order="F")
            final_strain[:, 0] = strain_temp[:, 0]
            final_strain[:, 1] = strain_temp[:, 2]
            final_strain[:, 2] = (strain_temp[:, 5] - strain_temp[:, 0]
                                  - strain_temp[:, 2])
            final_strain[:, 3] = -strain_temp[:, 4]
            final_strain[:, 4] = strain_temp[:, 1]
            final_strain[:, 5] = -strain_temp[:, 3]
            mesh.strain_buffer.add(id_elem, final_strain)
        else:
            final_strain = mesh.strain_buffer.get(id_elem)

        return final_strain

    def __get_displacement(self, mesh, id_elem, gll_point_ids, col_points_xi,
                           col_points_eta, xi, eta):
        if id_elem not in mesh.displ_buffer:
            utemp = np.zeros((mesh.ndumps, mesh.npol + 1, mesh.npol + 1, 3),
                             dtype=np.float64, order="F")

            mesh_dict = mesh.f.groups["Snapshots"].variables

            # Load displacement from all GLL points.
            for i, var in enumerate(["disp_s", "disp_p", "disp_z"]):
                if var not in mesh_dict:
                    continue
                temp = mesh_dict[var][:, gll_point_ids.flatten()]
                for ipol in xrange(mesh.npol + 1):
                    for jpol in xrange(mesh.npol + 1):
                        utemp[:, jpol, ipol, i] = temp[:, ipol * 5 + jpol]

            mesh.displ_buffer.add(id_elem, utemp)
        else:
            utemp = mesh.displ_buffer.get(id_elem)

        final_displacement = np.empty((utemp.shape[0], 3), order="F")

        for i in xrange(3):
            final_displacement[:, i] = spectral_basis.lagrange_interpol_2D_td(
                col_points_xi, col_points_eta, utemp[:, :, :, i], xi, eta)

        return final_displacement

    @property
    def dt(self):
        return self.parsed_mesh.dt

    @property
    def ndumps(self):
        return self.parsed_mesh.ndumps

    @property
    def background_model(self):
        return self.parsed_mesh.background_model

    @property
    def attenuation(self):
        return self.parsed_mesh.attenuation

    @property
    def sliprate(self):
        return self.parsed_mesh.stf_d_norm

    @property
    def slip(self):
        return self.parsed_mesh.stf

    def __str__(self):
        # Get the size of all netCDF files.
        filesize = 0
        for m in self.meshes:
            filesize += os.path.getsize(m.filename)
        filesize = sizeof_fmt(filesize)

        if self.reciprocal:
            if self.meshes.pz is not None and self.meshes.px is not None:
                components = 'vertical and horizontal'
            elif self.meshes.pz is None and self.meshes.px is not None:
                components = 'horizontal only'
            elif self.meshes.pz is not None and self.meshes.px is None:
                components = 'vertical only'
        else:
            components = '4 elemental moment tensors'

        return_str = (
            "AxiSEM {reciprocal} Green's function Database (v{format_v}) "
            "generated with these parameters:\n"
            "\tcomponents           : {components}\n"
            "{source_depth}"
            "\tvelocity model       : {velocity_model}\n"
            "\tattenuation          : {attenuation}\n"
            "\tdominant period      : {period:.3f} s\n"
            "\tdump type            : {dump_type}\n"
            "\texcitation type      : {excitation_type}\n"
            "\ttime step            : {dt:.3f} s\n"
            "\tsampling rate        : {sampling_rate:.3f} Hz\n"
            "\tnumber of samples    : {npts}\n"
            "\tseismogram length    : {length:.1f} s\n"
            "\tsource time function : {stf}\n"
            "\tsource shift         : {src_shift:.3f} s\n"
            "\tspatial order        : {spatial_order}\n"
            "\tmin/max radius       : {min_rad:.1f} - {max_rad:.1f} km\n"
            "\tPlanet radius        : {planet_rad:.1f} km\n"
            "\tmin/max distance     : {min_d:.1f} - {max_d:.1f} deg\n"
            "\ttime stepping scheme : {time_scheme}\n"
            "\tcompiler/user        : {compiler} by {user}\n"
            "\tdirectory            : {directory}\n"
            "\tsize of netCDF files : {filesize}\n"
            "\tgenerated by AxiSEM version {axisem_v} at {datetime}\n"
        ).format(
            reciprocal="reciprocal" if self.reciprocal else "forward",
            components=components,
            source_depth=(
                "\tsource depth         : %.2f km\n" %
                self.parsed_mesh.source_depth) if self.reciprocal is False
            else "",
            velocity_model=self.background_model,
            attenuation=self.attenuation,
            period=self.parsed_mesh.dominant_period,
            dump_type=self.dump_type,
            excitation_type=self.parsed_mesh.excitation_type,
            dt=self.dt,
            sampling_rate=1.0 / self.dt,
            npts=self.ndumps,
            length=self.dt * (self.ndumps - 1),
            stf=self.parsed_mesh.stf,
            src_shift=self.parsed_mesh.source_shift,
            spatial_order=self.parsed_mesh.npol,
            min_rad=self.parsed_mesh.kwf_rmin,
            max_rad=self.parsed_mesh.kwf_rmax,
            planet_rad=self.parsed_mesh.planet_radius / 1E3,
            min_d=self.parsed_mesh.kwf_colatmin,
            max_d=self.parsed_mesh.kwf_colatmax,
            time_scheme=self.parsed_mesh.time_scheme,
            directory=os.path.relpath(self.db_path),
            filesize=filesize,
            compiler=self.parsed_mesh.axisem_compiler,
            user=self.parsed_mesh.axisem_user,
            format_v=self.parsed_mesh.file_version,
            axisem_v=self.parsed_mesh.axisem_version,
            datetime=self.parsed_mesh.creation_time
        )
        return return_str


def sizeof_fmt(num):
    """
    Handy formatting for human readable filesizes.

    From http://stackoverflow.com/a/1094933/1657047
    """
    for x in ["bytes", "KB", "MB", "GB"]:
        if num < 1024.0 and num > -1024.0:
            return "%3.1f %s" % (num, x)
        num /= 1024.0
    return "%3.1f %s" % (num, "TB")
