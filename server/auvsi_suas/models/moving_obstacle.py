"""Moving obstacle model."""

import numpy as np
from datetime import timedelta
from django.conf import settings
from django.db import models
from django.utils import timezone
from scipy.interpolate import splrep, splev

from auvsi_suas.models import distance
from auvsi_suas.models import units
from auvsi_suas.models.uas_telemetry import UasTelemetry
from auvsi_suas.models.waypoint import Waypoint
from auvsi_suas.patches.simplekml_patch import AltitudeMode
from auvsi_suas.patches.simplekml_patch import Color
from auvsi_suas.patches.simplekml_patch import Types


class MovingObstacle(models.Model):
    """A moving obstacle that teams must avoid.

    Attributes:
        waypoints: The waypoints the obstacle attempts to follow.
        speed_avg: The average speed of the obstacle in knots.
        sphere_radius: The radius of the sphere in feet.
    """
    waypoints = models.ManyToManyField(Waypoint)
    speed_avg = models.FloatField()
    sphere_radius = models.FloatField()

    def __str__(self):
        """Descriptive text for use in displays."""
        return "MovingObstacle (pk:%s, speed:%s, radius:%s)" % (
            str(self.pk), str(self.speed_avg), str(self.sphere_radius))

    def get_waypoint_travel_time(self, waypoints, id_tm1, id_t):
        """Gets the travel time to the current waypoint from a previous.

        Args:
          waypoints: A set of sorted waypoints which define a path.
          id_tm1: The ID of the starting waypoint.
          id_t: The ID of the ending waypoint.
        Returns:
          Time to travel between the two waypoints in seconds. Returns None on
          error.
        """
        # Validate inputs
        if not waypoints:
            return None
        if len(waypoints) < 2:
            return None
        if id_tm1 is None or id_tm1 < 0 or id_tm1 >= len(waypoints):
            return None
        if id_t is None or id_t < 0 or id_t >= len(waypoints):
            return None
        if self.speed_avg <= 0:
            return None

        waypoint_t = waypoints[id_t]
        waypoint_tm1 = waypoints[id_tm1]
        waypoint_dist = waypoint_tm1.distance_to(waypoint_t)
        speed_avg_fps = units.knots_to_feet_per_second(self.speed_avg)
        waypoint_travel_time = waypoint_dist / speed_avg_fps

        return waypoint_travel_time

    def get_inter_waypoint_travel_times(self, waypoints):
        """Computes the travel times for the waypoints.

        Args:
            waypoints: A list of waypoints defining a circular path.
        Returns:
            A numpy array of travel times between waypoints. The first value is
            between waypoint 0 and 1, the last between N and 0.
        """
        num_waypoints = len(waypoints)
        travel_times = np.zeros(num_waypoints + 1)
        for waypoint_id in range(1, num_waypoints + 1):
            # Current intra waypoint travel time
            id_tm1 = (waypoint_id - 1) % num_waypoints
            id_t = waypoint_id % num_waypoints
            cur_travel_time = self.get_waypoint_travel_time(
                waypoints, id_tm1, id_t)
            travel_times[waypoint_id] = cur_travel_time

        return travel_times

    def get_waypoint_times(self, waypoint_travel_times):
        """Computes the time at which the obstacle will be at each waypoint.

        Args:
            waypoint_travel_time: The inter-waypoint travel times generated by
                get_inter_waypiont_travel_times() or equivalent.
        Returns:
            A numpy array of waypoint times.
        """
        total_time = 0
        num_paths = len(waypoint_travel_times)
        pos_times = np.zeros(num_paths)
        for path_id in range(num_paths):
            total_time += waypoint_travel_times[path_id]
            pos_times[path_id] = total_time

        return pos_times

    def get_spline_curve(self, waypoints):
        """Computes spline curve representation to match waypoints.

        Args:
            waypoints: The waypoints to calculate a spline curve from.
        Returns:
            A tuple (total_travel_time, spline_reps) where total_travel_time is
            the total time to complete a circuit, and spline_reps is a list of
            tck values generated from spline creation. The list is ordered
            latitude, longitude, altitude.
        """
        num_waypoints = len(waypoints)

        # Store waypoint data for interpolation
        positions = np.zeros((num_waypoints + 1, 3))
        for waypoint_id in range(num_waypoints):
            cur_waypoint = waypoints[waypoint_id]
            cur_position = cur_waypoint.position
            cur_gps_pos = cur_position.gps_position
            positions[waypoint_id, 0] = cur_gps_pos.latitude
            positions[waypoint_id, 1] = cur_gps_pos.longitude
            positions[waypoint_id, 2] = cur_position.altitude_msl

        # Get the intra waypoint travel times
        waypoint_travel_times = self.get_inter_waypoint_travel_times(waypoints)
        # Get the waypoint times
        pos_times = self.get_waypoint_times(waypoint_travel_times)
        total_travel_time = pos_times[len(pos_times) - 1]

        # Create spline representation
        spline_k = 3 if num_waypoints >= 3 else 2  # Cubic if enough points
        spline_reps = []
        for iter_dim in range(3):
            tck = splrep(pos_times, positions[:, iter_dim], k=spline_k, per=1)
            spline_reps.append(tck)

        return (total_travel_time, spline_reps)

    def get_position(self, cur_time=None):
        """Gets the current position for the obstacle.

        Args:
          cur_time: The current time as datetime with time zone.
        Returns:
          Returns a tuple (latitude, longitude, altitude_msl) for the obstacle
          at the given time.
        """
        if cur_time is None:
            cur_time = timezone.now()

        # Get waypoints
        if hasattr(self, 'preprocessed_waypoints'):
            waypoints = self.preprocessed_waypoints
        else:
            # Load waypoints for obstacle, filter for consecutive duplicates
            all_wpts = self.waypoints.order_by('order')
            waypoints = [
                all_wpts[i] for i in range(len(all_wpts))
                if i == 0 or all_wpts[i].distance_to(all_wpts[i - 1]) != 0
            ]
            self.preprocessed_waypoints = waypoints

        # Waypoint counts of 0 or 1 can skip calc, so can no speed
        num_waypoints = len(waypoints)
        if num_waypoints == 0:
            return (0, 0, 0)  # Undefined position
        elif num_waypoints == 1 or self.speed_avg <= 0:
            wpt = waypoints[0]
            return (wpt.position.gps_position.latitude,
                    wpt.position.gps_position.longitude,
                    wpt.position.altitude_msl)

        # Get spline representation
        if hasattr(self, 'preprocessed_spline_curve'):
            spline_curve = self.preprocessed_spline_curve
        else:
            spline_curve = self.get_spline_curve(waypoints)
            self.preprocessed_spline_curve = spline_curve
        (total_travel_time, spline_reps) = spline_curve

        # Sample spline at current time
        epoch_time = timezone.now().replace(
            year=1970,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0)
        cur_time_sec = (cur_time - epoch_time).total_seconds()
        cur_path_time = np.mod(cur_time_sec, total_travel_time)
        latitude = float(splev(cur_path_time, spline_reps[0]))
        longitude = float(splev(cur_path_time, spline_reps[1]))
        altitude_msl = float(splev(cur_path_time, spline_reps[2]))

        return (latitude, longitude, altitude_msl)

    def contains_pos(self, obst_lat, obst_lon, obst_alt, aerial_pos):
        """Whether the pos is contained within the obstacle's pos.

        Args:
            obst_lat: The latitude of the obstacle.
            obst_lon: The longitude of the obstacle.
            obst_alt: The altitude of the obstacle.
            aerial_pos: The position to test.
        Returns:
            Whether the given position is inside the obstacle.
        """
        dist_to_center = distance.distance_to(
            obst_lat, obst_lon, obst_alt, aerial_pos.gps_position.latitude,
            aerial_pos.gps_position.longitude, aerial_pos.altitude_msl)
        return dist_to_center <= self.sphere_radius

    def evaluate_collision_with_uas(self, uas_telemetry_logs):
        """Evaluates whether the Uas logs indicate a collision.

        Args:
            uas_telemetry_logs: A list of UasTelemetry logs sorted by timestamp
                for which to evaluate.
        Returns:
            Whether a UAS telemetry log reported indicates a collision with the
            obstacle.
        """
        for log in UasTelemetry.interpolate(uas_telemetry_logs):
            (lat, lon, alt) = self.get_position(log.timestamp)
            if self.contains_pos(lat, lon, alt, log.uas_position):
                return True

        return False

    def json(self, time=None):
        """Obtain a JSON style representation of object."""
        (latitude, longitude, altitude_msl) = self.get_position(cur_time=time)
        data = {
            'latitude': latitude,
            'longitude': longitude,
            'altitude_msl': altitude_msl,
            'sphere_radius': self.sphere_radius
        }
        return data

    def kml(self, time_periods, kml, kml_doc):
        """
        Appends kml nodes describing the given user's flight as described
        by the log array given. No nodes are added if less than two log
        entries are given.

        Args:
            time_periods: The time period over which to generate positions.
            kml: A simpleKML Container to which the flight data will be added
            kml_doc: The simpleKML Document to which schemas will be added
        Returns:
            None
        """
        # KML Compliant Datetime Formatter
        kml_datetime_format = "%Y-%m-%dT%H:%M:%S.%fZ"
        icon = 'http://maps.google.com/mapfiles/kml/shapes/airports.png'

        # Generate track data for all time periods.
        coords = []
        when = []
        ranges = []
        for period in time_periods:
            t = period.start
            while t < period.end:
                (lat, lon, alt) = self.get_position(t)
                coords.append((lon, lat, units.feet_to_meters(alt)))
                when.append(t.strftime(kml_datetime_format))
                t += timedelta(milliseconds=100)

        # Create a new track in the folder
        trk = kml.newgxtrack(name='Obstacle Path {}'.format(self.id))
        trk.altitudemode = AltitudeMode.absolute

        # TODO: Add back proximity information lost when fixing #316.

        # Append flight data
        trk.newwhen(when)
        trk.newgxcoord(coords)

        # Set styling
        trk.extrude = 1  # Extend path to ground
        trk.style.linestyle.width = 2
        trk.style.linestyle.color = Color.red
        trk.iconstyle.icon.href = icon

    @classmethod
    def live_kml(cls, kml, timespan, resolution=100):
        """
        Appends kml nodes describing current paths of the obstacles

        Args:
            kml: A simpleKML Container to which the obstacle data will be added
            timespan: A timedelta to look backwards in time
            resolution: integer number of milliseconds between obstacle positions
        Returns:
            None
        """

        def track(obstacle, span, dt):
            curr = timezone.now()
            last = curr - span
            time = curr
            while time >= last:
                yield obstacle.get_position(time)
                time -= dt

        for obstacle in MovingObstacle.objects.all():
            dt = timedelta(milliseconds=resolution)
            linestring = kml.newlinestring(name="Obstacle")
            coords = []
            for pos in track(obstacle, timespan, dt):
                # Longitude, Latitude, Altitude
                coord = (pos[1], pos[0], units.feet_to_meters(pos[2]))
                coords.append(coord)
            linestring.coords = coords
            linestring.altitudemode = AltitudeMode.absolute
            linestring.extrude = 1
            linestring.style.linestyle.color = Color.red
            linestring.style.polystyle.color = Color.changealphaint(
                100, Color.red)
