# TODO - Future Improvements

## Circuit Breaker Pattern for USBGuard Client

Add a circuit breaker pattern to the USBGuardClient to prevent overwhelming the USBGuard daemon when it's consistently failing.

### Implementation Details

1. **States**:
   - CLOSED: Normal operation, requests pass through
   - OPEN: Failure threshold reached, requests fail fast without calling D-Bus
   - HALF-OPEN: Testing if service recovered, allows limited requests

2. **Configuration**:
   - Failure threshold: 5 consecutive failures
   - Timeout: 30 seconds before transitioning from OPEN to HALF-OPEN
   - Reset failure count on successful calls

3. **Benefits**:
   - Prevents thrashing when USBGuard daemon is unresponsive
   - Provides faster failure response (no waiting for timeouts)
   - Enables automatic recovery testing
   - Keeps application responsive during daemon outages

4. **Integration Points**:
   - Wrap D-Bus proxy calls in `_call_with_circuit_breaker()` method
   - Apply to: list_devices, apply_device_policy, list_rules, remove_rule
   - Work alongside existing exponential backoff reconnection logic

### Example Usage
```python
def list_devices(self, query: str = "match") -> list[Device]:
    if not self._devices_proxy:
        return []
    try:
        raw = self._call_with_circuit_breaker(
            self._devices_proxy.listDevices, query
        )
        # ... rest of method
```

### Related Files
- src/usbguard_gui/dbus_client.py (main implementation)
- Potentially update tests to cover circuit breaker behavior
