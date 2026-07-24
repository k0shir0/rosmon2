^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package rosmon2
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Forthcoming
-----------
* Fixed the ``d`` (gdb) node action leaving every later restart wrapped in gdb.
* Fixed function keys being missed when a terminal read split their escape
  sequence after the third byte.
* Released the process log file and control socket when a launch failed before
  the run loop started.
* Rejected control runtime directories owned by, or writable to, other users,
  and created the default process log with an unpredictable private path.
* Kept a failing event subscriber or a torn-down event socket from stopping
  process supervision.
* Reported ``wait`` on a target that never appeared instead of blocking for the
  full timeout, and reported log records with the same absolute node name as
  ``status``.
* Reduced per-line output overhead by classifying each line once and caching
  the status-bar label styling.
* Added a dependency-free MCP server for inspecting and controlling sessions.
* Added named control sessions, structured JSON output, log queries, event
  streaming, and deterministic state waits.
* Added node search, namespace grouping, and per-node process controls.
* Added a terminal process monitor backed by the native ROS 2 launch engine.
* Contributors: Gibson
