import os
import sys
import time
import socket

# Ensure src is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from telemetry_router import TelemetryRouter, DroneTelemetryState
from pymavlink.dialects.v20 import common as mavlink2

def run_loopback_test():
    print("=" * 60)
    print(" MAVLink Telemetry Pipeline Loopback Self-Test")
    print("=" * 60)

    # 1. Start TelemetryRouter on custom test ports to avoid collisions
    phone_port = 14591
    mp_port = 14590
    mp_local_port = 14592

    router = TelemetryRouter(
        phone_bind_ip="127.0.0.1",
        phone_bind_port=phone_port,
        mp_host="127.0.0.1",
        mp_port=mp_port,
        mp_local_port=mp_local_port,
        enable_tcp=False,
        on_log=lambda m: print(f"  [Router] {m}")
    )
    router.start()
    time.sleep(0.1)

    # 2. Setup mock Mission Planner receiver socket on mp_port (14590)
    mock_mp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mock_mp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    mock_mp_sock.bind(("127.0.0.1", mp_port))
    mock_mp_sock.settimeout(1.0)

    # 3. Setup mock Phone transmitter socket
    mock_phone_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mock_phone_sock.settimeout(1.0)

    try:
        # Create a MAVLink Heartbeat packet
        mav = mavlink2.MAVLink(None)
        # Heartbeat: type=QUADROTOR(2), autopilot=ARDUPILOTMEGA(3), base_mode=ARMED(128), custom_mode=LOITER(5), system_status=4
        hb = mav.heartbeat_encode(
            type=mavlink2.MAV_TYPE_QUADROTOR,
            autopilot=mavlink2.MAV_AUTOPILOT_ARDUPILOTMEGA,
            base_mode=mavlink2.MAV_MODE_FLAG_SAFETY_ARMED,
            custom_mode=5,  # LOITER
            system_status=mavlink2.MAV_STATE_ACTIVE
        )
        packet_bytes = hb.pack(mav)

        print("\n[Step 1] Mock Phone sending MAVLink HEARTBEAT to Router on port", phone_port)
        mock_phone_sock.sendto(packet_bytes, ("127.0.0.1", phone_port))

        # Check if Mission Planner receives it
        received_data, from_addr = mock_mp_sock.recvfrom(2048)
        print(f"[Step 2] Mock Mission Planner successfully received {len(received_data)} bytes from {from_addr}")
        assert len(received_data) == len(packet_bytes), "Packet size mismatch!"

        # Allow router to process
        time.sleep(0.1)
        state = router.get_state()
        print(f"[Step 3] Router Internal State Verified:")
        print(f"         Connected:    {state.connected}")
        print(f"         Armed:        {state.armed}")
        print(f"         Flight Mode:  {state.flight_mode}")
        print(f"         SysID/CompID: {state.sys_id}/{state.comp_id}")
        assert state.connected, "Router did not register connected state!"
        assert state.armed, "Router did not register armed state!"
        assert state.flight_mode == "LOITER", f"Expected LOITER, got {state.flight_mode}"

        # 4. Test Downlink: Mock Mission Planner sending response back to Router
        print("\n[Step 4] Mock Mission Planner replying with PARAM_REQUEST_LIST...")
        req = mav.param_request_list_encode(target_system=1, target_component=1)
        req_bytes = req.pack(mav)
        mock_mp_sock.sendto(req_bytes, from_addr)

        # Mock Phone should receive this downlink
        downlink_data, downlink_addr = mock_phone_sock.recvfrom(2048)
        print(f"[Step 5] Mock Phone successfully received {len(downlink_data)} return bytes from Router!")
        assert len(downlink_data) == len(req_bytes), "Downlink packet size mismatch!"

        print("\n" + "=" * 60)
        print(" [SUCCESS] BIDIRECTIONAL MAVLINK TELEMETRY PIPELINE TEST PASSED 100%!")
        print("=" * 60)

    finally:
        router.stop()
        mock_mp_sock.close()
        mock_phone_sock.close()

if __name__ == "__main__":
    run_loopback_test()
