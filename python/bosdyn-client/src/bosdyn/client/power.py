# Copyright (c) 2019 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""For clients to the power command service."""
import collections
from concurrent.futures import TimeoutError
import time

from bosdyn.client.common import BaseClient
from bosdyn.client.common import (error_factory, handle_unset_status_error,
                                  handle_common_header_errors, handle_lease_use_result_errors)
from bosdyn.client.exceptions import Error, ResponseError, InternalServerError

from bosdyn.api import power_pb2
from bosdyn.api import power_service_pb2_grpc
from bosdyn.api import robot_command_pb2
from bosdyn.api import robot_state_pb2

from .lease import add_lease_wallet_processors


class PowerResponseError(ResponseError):
    """General class of errors for Power service."""


class ShorePowerConnectedError(PowerResponseError):
    """Robot cannot be powered on while on wall power."""


class BatteryMissingError(PowerResponseError):
    """Battery not inserted into robot."""


class CommandInProgressError(PowerResponseError):
    """Power command cannot be overwritten."""


class EstoppedError(PowerResponseError):
    """Cannot power on while one estopped.

       Inspect EStopState for more info."""


class FaultedError(PowerResponseError):
    """Cannot power on due to a fault.

       Inspect FaultState for more info."""


class PowerError(Error):
    """General class of errors to handle non-response non-grpc errors."""


class CommandTimedOutError(PowerError):
    """Timed out waiting for SUCCESS response from power command."""


class PowerClient(BaseClient):
    """A client for enabling / disabling robot motor power.
    Commands are non blocking. Clients are expected to issue a power command and then periodically
    check the status of this command.
    This service requires ownership over the robot, in the form of a lease.
    """
    default_authority = 'power.spot.robot'
    default_service_name = 'power'
    service_type = 'bosdyn.api.PowerService'

    def __init__(self):
        super(PowerClient, self).__init__(power_service_pb2_grpc.PowerServiceStub)

    def update_from(self, other):
        super(PowerClient, self).update_from(other)
        if self.lease_wallet:
            add_lease_wallet_processors(self, self.lease_wallet)

    def power_command(self, request, lease=None, **kwargs):
        """Issue a power request to the robot."""
        req = self._power_command_request(lease, request)
        return self.call(self._stub.PowerCommand, req, None, _power_command_error_from_response,
                         **kwargs)

    def power_command_async(self, request, lease=None, **kwargs):
        """Async version of power_command()."""
        req = self._power_command_request(lease, request)
        return self.call_async(self._stub.PowerCommand, req, None,
                               _power_command_error_from_response, **kwargs)

    def power_command_feedback(self, power_command_id, **kwargs):
        """Check the status of a previously issued power command."""
        req = self._power_command_feedback_request(power_command_id)
        return self.call(self._stub.PowerCommandFeedback, req, _power_status_from_response,
                         _power_feedback_error_from_response, **kwargs)

    def power_command_feedback_async(self, power_command_id, **kwargs):
        """Async version of power_command_feedback()"""
        req = self._power_command_feedback_request(power_command_id)
        return self.call_async(self._stub.PowerCommandFeedback, req, _power_status_from_response,
                               _power_feedback_error_from_response, **kwargs)

    @staticmethod
    def _power_command_request(lease, request):
        return power_pb2.PowerCommandRequest(lease=lease, request=request)

    @staticmethod
    def _power_command_feedback_request(power_command_id):
        return power_pb2.PowerCommandFeedbackRequest(power_command_id=power_command_id)


@handle_common_header_errors
@handle_lease_use_result_errors
def _power_command_error_from_response(response):
    return _power_status_error_from_response(response)


@handle_common_header_errors
def _power_feedback_error_from_response(response):
    return _power_status_error_from_response(response)


_STATUS_TO_ERROR = collections.defaultdict(lambda: (ResponseError, None))
_STATUS_TO_ERROR.update({
    power_pb2.STATUS_SUCCESS: (None, None),
    power_pb2.STATUS_IN_PROGRESS: (None, None),
    power_pb2.STATUS_SHORE_POWER_CONNECTED: (ShorePowerConnectedError,
                                             ShorePowerConnectedError.__doc__),
    power_pb2.STATUS_BATTERY_MISSING: (BatteryMissingError, BatteryMissingError.__doc__),
    power_pb2.STATUS_COMMAND_IN_PROGRESS: (CommandInProgressError, CommandInProgressError.__doc__),
    power_pb2.STATUS_ESTOPPED: (EstoppedError, EstoppedError.__doc__),
    power_pb2.STATUS_FAULTED: (FaultedError, FaultedError.__doc__),
    power_pb2.STATUS_INTERNAL_ERROR: (InternalServerError, InternalServerError.__doc__),
})


@handle_unset_status_error(unset='STATUS_UNKNOWN', statustype=power_pb2)
def _power_status_error_from_response(response):
    """Return a custom exception based on response, None if no error."""
    return error_factory(response, response.status,
                         status_to_string=power_pb2.PowerCommandStatus.Name,
                         status_to_error=_STATUS_TO_ERROR)


def _power_status_from_response(response):
    return response.status


def safe_power_off(command_client, state_client, timeout_sec=30, update_frequency=1.0, **kwargs):
    """Power off robot safely. This function blocks until robot safely powers off. This means the
    robot will attempt to sit before powering off.

    Args:
        command_client (RobotCommandClient): client for calling RobotCommandService safe power off.
        state_client (StateCommandClient): client for calling RobotStateClient for monitoring power
            state.
        timeout_sec (float): Max time this function will block for.

    Raises:
        Error: Throws on error.
    """
    start_time = time.time()
    end_time = start_time + timeout_sec
    update_time = 1.0 / update_frequency

    full_body_command = robot_command_pb2.FullBodyCommand.Request(
        safe_power_off_request=robot_command_pb2.SafePowerOffCommand.Request())
    command = robot_command_pb2.RobotCommand(full_body_command=full_body_command)
    command_client.robot_command(command=command, **kwargs)

    while time.time() < end_time:
        time_until_timeout = end_time - time.time()
        start_call_time = time.time()
        future = state_client.get_robot_state_async(**kwargs)
        try:
            response = future.result(timeout=time_until_timeout)
            if response.power_state.motor_power_state == robot_state_pb2.PowerState.STATE_OFF:
                return
        except TimeoutError:
            raise CommandTimedOutError
        call_time = time.time() - start_call_time
        sleep_time = max(0.0, update_time - call_time)
        time.sleep(sleep_time)
    raise CommandTimedOutError


def power_on(power_client, timeout_sec=30, update_frequency=1.0, **kwargs):
    """Power on robot. This function blocks until robot powers on.

    Args:
        client (PowerClient): client for calling power service.
        timeout_sec (float): Max time this function will block for.

    Raises:
        Error: Throws on error.
    """
    request = power_pb2.PowerCommandRequest.REQUEST_ON
    _power_command(power_client, request, timeout_sec, update_frequency, **kwargs)


def power_off(client, timeout_sec=30, update_frequency=1.0, **kwargs):
    """Power off robot immediately. This function blocks until robot powers off. This will not put
    robot in a safe state.

    Args:
        client (PowerClient): client for calling power service.
        timeout_sec (float): Max time this function will block for.

    Raises:
        Error: Throws on error.
    """
    request = power_pb2.PowerCommandRequest.REQUEST_OFF
    _power_command(client, request, timeout_sec, update_frequency, **kwargs)


def _power_command(power_client, request, timeout_sec=30, update_frequency=1.0, **kwargs):
    """Helper function to issue command to power client."""
    start_time = time.time()
    end_time = start_time + timeout_sec
    update_time = 1.0 / update_frequency

    response = power_client.power_command(request, **kwargs)
    power_command_id = response.power_command_id
    while time.time() < end_time:
        # user specified timeout as possible, and differentiate between GRPC timeouts and command
        # completion timeouts.
        time_until_timeout = end_time - time.time()
        start_call_time = time.time()
        future = power_client.power_command_feedback_async(power_command_id, **kwargs)
        try:
            response = future.result(timeout=time_until_timeout)
            if response == power_pb2.STATUS_SUCCESS:
                return
        except TimeoutError:
            raise CommandTimedOutError
        call_time = time.time() - start_call_time
        sleep_time = max(0.0, update_time - call_time)
        time.sleep(sleep_time)
    raise CommandTimedOutError


def is_powered_on(state_client, **kwargs):
    """Returns true if robot is powered on, false otherwise.

    Raises:
        Error: ResponseError.
    """
    response = state_client.get_robot_state(**kwargs)
    return response.power_state.motor_power_state == robot_state_pb2.PowerState.STATE_ON
