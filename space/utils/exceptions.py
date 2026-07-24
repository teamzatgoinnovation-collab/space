"""Shared Space exceptions."""


class SpaceError(Exception):
	"""Base Space control-plane error."""


class SpacePermissionError(SpaceError):
	pass


class SpaceCapacityError(SpaceError):
	pass


class SpaceJobError(SpaceError):
	pass
