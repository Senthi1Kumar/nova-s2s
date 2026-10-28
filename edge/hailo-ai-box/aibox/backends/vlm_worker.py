"""Child process that owns the in-process-style Qwen-VL.

The chip keeps a VLM's weights resident after `VLM.release()` for the life of the process that
loaded it (3.3 GB of the 7.2 GB device RAM), so swapping the GenAI slot to hailo-ollama is only
possible if the VLM lives in a process that can exit. Protocol over a multiprocessing Pipe:

  parent -> child: ("gen", prompt, frames, max_tokens) | ("stop",) | ("quit",)
  child -> parent: ("ready", frame_shape, frame_dtype) | ("tok", text) | ("done",) | ("error", text)
"""

from __future__ import annotations

from multiprocessing.connection import Connection


def worker_main(conn: Connection, hef_path: str) -> None:
    vd = vlm = None
    try:
        from hailo_platform import VDevice
        from hailo_platform.genai import VLM

        p = VDevice.create_params()
        p.group_id = "SHARED"
        vd = VDevice(p)
        vlm = VLM(vd, hef_path)
        conn.send(("ready", tuple(int(x) for x in vlm.input_frame_shape()),
                   str(vlm.input_frame_format_type())))
    except BaseException as exc:  # noqa: BLE001
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        conn.close()
        return
    quit_requested = False
    try:
        while not quit_requested:
            try:
                msg = conn.recv()
            except EOFError:
                break
            if msg[0] == "quit":
                break
            if msg[0] != "gen":
                continue
            _, prompt, frames, max_tokens = msg
            try:
                with vlm.generate(prompt=prompt, frames=frames, temperature=0.1, seed=42,
                                  max_generated_tokens=max_tokens) as gen:
                    for chunk in gen:
                        conn.send(("tok", chunk))
                        if conn.poll():
                            nxt = conn.recv()
                            if nxt[0] == "quit":
                                quit_requested = True
                            break  # "stop" (or quit): abandon this generation
            except Exception as exc:  # noqa: BLE001
                conn.send(("error", f"{type(exc).__name__}: {exc}"))
            finally:
                try:
                    vlm.clear_context()
                except Exception:  # noqa: BLE001
                    pass
                conn.send(("done",))
    finally:
        for obj, fn in ((vlm, "release"), (vd, "release")):
            try:
                getattr(obj, fn)()
            except Exception:  # noqa: BLE001
                pass
        conn.close()
