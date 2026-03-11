/**
 * ConnectionHealthManager — pure logic, no UI (Task 5.5)
 *
 * Tracks WebRTC session health: session age, event recency, speech
 * recency, disconnect history, reconnect count, and reconnect strategy.
 */
class ConnectionHealthManager {
  /**
   * @param {{
   *   strategy?: 'manual'|'auto_immediate'|'auto_delayed'|'proactive',
   *   callbacks?: {
   *     onHealthStatusChange?: (status: string) => void,
   *     onSessionWarning?: (remainingMs: number) => void,
   *     onIdleWarning?: (idleMs: number) => void,
   *     onStaleConnection?: (timeSinceLastEventMs: number) => void,
   *     onDisconnectDetected?: (reason: string, metrics: object) => void,
   *   }
   * }} options
   */
  constructor({ strategy = 'manual', callbacks = {} } = {}) {
    this.strategy = strategy;
    this.reconnectCount = 0;
    this.disconnectHistory = [];

    // Internal timestamps (null until startSession is called)
    this._sessionStart = null;
    this._lastEventTime = null;
    this._lastSpeechTime = null;
    this._lastActivityTime = null;
    this._isConnected = false;
    this._sessionCount = 0;

    // ICE state history (bounded, max 100)
    this._connectionStateHistory = [];

    // Event log (bounded, max 50)
    this._eventLog = [];

    // Last known health status (for change detection in monitoring)
    this._lastHealthStatus = null;

    // Last stale warning timestamp (for rate limiting)
    this._lastStaleWarningTime = null;

    // Monitoring interval ID
    this._monitoringInterval = null;

    // Callbacks
    this._callbacks = Object.assign({}, callbacks);
  }

  // -------------------------------------------------------------------------
  // Lifecycle
  // -------------------------------------------------------------------------

  /** Call when a new session starts (or reconnects). */
  startSession() {
    const now = Date.now();

    // Auto-increment reconnect count on second+ session
    if (this._sessionCount > 0) {
      this.reconnectCount++;
    }
    this._sessionCount++;

    this._sessionStart = now;
    this._lastEventTime = now;
    this._lastSpeechTime = now;
    this._lastActivityTime = now;
    this._isConnected = true;

    this.logEvent('session_started');
    this.startMonitoring();
  }

  /**
   * Call on disconnect. Logs metrics, records disconnect history.
   * @param {'idle_timeout'|'session_limit'|'connection_failed'|'data_channel_closed'|'stale_connection'|'network_error'|'user_initiated'|'unknown'} [reason]
   */
  endSession(reason) {
    const resolvedReason = reason || this.analyzeDisconnectReason();
    const now = Date.now();
    const metrics = this.getStatus();

    this.disconnectHistory.push({
      timestamp: now,
      reason: resolvedReason,
      sessionAge: metrics.sessionAge,
      reconnectCount: this.reconnectCount,
    });

    this._isConnected = false;
    this.logEvent('session_ended');
    this.stopMonitoring();

    if (this._callbacks.onDisconnectDetected) {
      this._callbacks.onDisconnectDetected(resolvedReason, metrics);
    }

    console.debug('[ConnectionHealthManager] Session ended:', resolvedReason, metrics);
  }

  // -------------------------------------------------------------------------
  // Recording
  // -------------------------------------------------------------------------

  /** Record that a realtime event was received (heartbeat for staleness check). */
  recordEvent() {
    this._lastEventTime = Date.now();
  }

  /** Record that the user or assistant produced speech. */
  recordSpeech() {
    this._lastSpeechTime = Date.now();
    this.recordActivity();
  }

  /** Record any user input activity. */
  recordActivity() {
    this._lastActivityTime = Date.now();
  }

  /**
   * Record an ICE connection state transition (bounded history, max 100).
   * @param {string} state
   */
  recordConnectionState(state) {
    this._connectionStateHistory.push({ state, timestamp: Date.now() });
    if (this._connectionStateHistory.length > 100) {
      this._connectionStateHistory.shift();
    }
    this.logEvent('ice_' + state);
  }

  // -------------------------------------------------------------------------
  // Event log
  // -------------------------------------------------------------------------

  /**
   * Append an event to the bounded event log (max 50).
   * @param {string} eventType
   */
  logEvent(eventType) {
    this._eventLog.push({ eventType, timestamp: Date.now() });
    if (this._eventLog.length > 50) {
      this._eventLog.shift();
    }
  }

  /**
   * Return event log entries with formatted age strings.
   * @returns {Array<{ eventType: string, timestamp: number, age: string }>}
   */
  getEventLog() {
    const now = Date.now();
    return this._eventLog.map((entry) => ({
      eventType: entry.eventType,
      timestamp: entry.timestamp,
      age: this.formatDuration(now - entry.timestamp) + ' ago',
    }));
  }

  // -------------------------------------------------------------------------
  // Status
  // -------------------------------------------------------------------------

  /**
   * Returns a snapshot of current session health.
   * @returns {{
   *   sessionAge: number|null,
   *   timeSinceEvent: number,
   *   timeSinceSpeech: number,
   *   warnings: string[],
   *   reconnectCount: number,
   *   healthStatus: 'healthy'|'warning'|'critical'|'disconnected',
   * }}
   */
  getStatus() {
    const now = Date.now();

    const sessionAge =
      this._sessionStart !== null ? (now - this._sessionStart) / 1000 : null;
    const timeSinceEvent =
      this._lastEventTime !== null ? (now - this._lastEventTime) / 1000 : 0;
    const timeSinceSpeech =
      this._lastSpeechTime !== null ? (now - this._lastSpeechTime) / 1000 : 0;

    const warnings = [];
    if (timeSinceEvent > 30) warnings.push('stale');
    if (timeSinceSpeech > 120) warnings.push('idle');
    if (sessionAge !== null && sessionAge > 55 * 60) warnings.push('session_limit');

    return {
      sessionAge,
      timeSinceEvent,
      timeSinceSpeech,
      warnings,
      reconnectCount: this.reconnectCount,
      healthStatus: this.getHealthStatus(),
    };
  }

  /**
   * Returns 4-state health status based on current metrics.
   * @returns {'healthy'|'warning'|'critical'|'disconnected'}
   */
  getHealthStatus() {
    if (!this._isConnected) return 'disconnected';

    const now = Date.now();
    const timeSinceEventMs =
      this._lastEventTime !== null ? now - this._lastEventTime : 0;
    const timeSinceSpeechMs =
      this._lastSpeechTime !== null ? now - this._lastSpeechTime : 0;
    const sessionAgeMs =
      this._sessionStart !== null ? now - this._sessionStart : 0;

    // Critical: stale (no events >30s) or approaching session limit (>55min)
    if (timeSinceEventMs > 30 * 1000 || sessionAgeMs > 55 * 60 * 1000) {
      return 'critical';
    }

    // Warning: idle (>2min) or session >45min
    if (timeSinceSpeechMs > 2 * 60 * 1000 || sessionAgeMs > 45 * 60 * 1000) {
      return 'warning';
    }

    return 'healthy';
  }

  // -------------------------------------------------------------------------
  // Disconnect reason inference
  // -------------------------------------------------------------------------

  /**
   * Infer why the session disconnected (takes a sessionAge parameter for backward compat).
   * @param {number} [sessionAge]  Session age in seconds at time of disconnect.
   * @returns {'session_limit'|'idle_timeout'|'network_error'}
   */
  inferDisconnectReason(sessionAge) {
    if (sessionAge == null) {
      sessionAge =
        this._sessionStart !== null
          ? (Date.now() - this._sessionStart) / 1000
          : 0;
    }
    if (sessionAge >= 58 * 60) return 'session_limit';

    const timeSinceSpeech =
      this._lastSpeechTime !== null
        ? (Date.now() - this._lastSpeechTime) / 1000
        : Infinity;
    if (timeSinceSpeech >= 120) return 'idle_timeout';

    return 'network_error';
  }

  /**
   * Analyze current metrics to classify disconnect cause (no-argument version).
   * @returns {'session_limit'|'idle_timeout'|'stale_connection'|'network_error'|'unknown'}
   */
  analyzeDisconnectReason() {
    const now = Date.now();
    const sessionAge =
      this._sessionStart !== null ? (now - this._sessionStart) / 1000 : 0;
    const timeSinceEvent =
      this._lastEventTime !== null ? (now - this._lastEventTime) / 1000 : 0;
    const timeSinceSpeech =
      this._lastSpeechTime !== null ? (now - this._lastSpeechTime) / 1000 : 0;

    if (sessionAge >= 58 * 60) return 'session_limit';
    if (timeSinceSpeech >= 120 && timeSinceEvent >= 120) return 'idle_timeout';
    if (timeSinceEvent >= 30) return 'stale_connection';
    if (this._sessionStart !== null) return 'network_error';
    return 'unknown';
  }

  // -------------------------------------------------------------------------
  // Monitoring
  // -------------------------------------------------------------------------

  /** Start the 5-second monitoring interval. */
  startMonitoring() {
    this.stopMonitoring(); // clear any existing interval
    this._monitoringInterval = setInterval(() => {
      this._runMonitoringTick();
    }, 5000);
  }

  /** Stop the monitoring interval. */
  stopMonitoring() {
    if (this._monitoringInterval !== null) {
      clearInterval(this._monitoringInterval);
      this._monitoringInterval = null;
    }
  }

  /** @private Run one monitoring tick. */
  _runMonitoringTick() {
    if (!this._isConnected) return;

    const now = Date.now();
    const sessionAgeMs =
      this._sessionStart !== null ? now - this._sessionStart : 0;
    const timeSinceEventMs =
      this._lastEventTime !== null ? now - this._lastEventTime : 0;
    const timeSinceSpeechMs =
      this._lastSpeechTime !== null ? now - this._lastSpeechTime : 0;

    const SESSION_LIMIT_MS = 60 * 60 * 1000;
    const SESSION_WARN_MS  = 55 * 60 * 1000;
    const IDLE_WARN_MS     =  2 * 60 * 1000;
    const STALE_WARN_MS    = 30 * 1000;
    const STALE_RATE_MS    = 30 * 1000;

    // Session limit warning
    if (sessionAgeMs > SESSION_WARN_MS && this._callbacks.onSessionWarning) {
      this._callbacks.onSessionWarning(SESSION_LIMIT_MS - sessionAgeMs);
    }

    // Idle warning
    if (timeSinceSpeechMs > IDLE_WARN_MS && this._callbacks.onIdleWarning) {
      this._callbacks.onIdleWarning(timeSinceSpeechMs);
    }

    // Stale connection — rate-limited to once per 30s
    if (timeSinceEventMs > STALE_WARN_MS && this._callbacks.onStaleConnection) {
      const timeSinceLastWarn = this._lastStaleWarningTime
        ? now - this._lastStaleWarningTime
        : Infinity;
      if (timeSinceLastWarn >= STALE_RATE_MS) {
        this._lastStaleWarningTime = now;
        this._callbacks.onStaleConnection(timeSinceEventMs);
      }
    }

    // Health status change detection
    const currentStatus = this.getHealthStatus();
    if (currentStatus !== this._lastHealthStatus) {
      this._lastHealthStatus = currentStatus;
      if (this._callbacks.onHealthStatusChange) {
        this._callbacks.onHealthStatusChange(currentStatus);
      }
    }
  }

  // -------------------------------------------------------------------------
  // Utilities
  // -------------------------------------------------------------------------

  /** Reset all state back to construction defaults. */
  reset() {
    this.stopMonitoring();
    this.reconnectCount = 0;
    this.disconnectHistory = [];
    this._sessionStart = null;
    this._lastEventTime = null;
    this._lastSpeechTime = null;
    this._lastActivityTime = null;
    this._isConnected = false;
    this._sessionCount = 0;
    this._connectionStateHistory = [];
    this._eventLog = [];
    this._lastHealthStatus = null;
    this._lastStaleWarningTime = null;
  }

  /**
   * Update callbacks after construction.
   * @param {object} callbacks
   */
  setCallbacks(callbacks) {
    this._callbacks = Object.assign({}, this._callbacks, callbacks);
  }

  /**
   * Format milliseconds to a human-readable duration string.
   * @param {number} ms
   * @returns {string}  e.g. "2m 30s", "1h 5m", "45s"
   */
  formatDuration(ms) {
    if (ms == null || ms < 0) return '—';
    const totalSeconds = Math.floor(ms / 1000);
    const hours   = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0)   return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${seconds}s`;
    return `${seconds}s`;
  }
}
