import { useEffect, useRef, useState } from "react";

// In-browser mic recording with a live volume meter and local playback.
// Mirrors the old Streamlit demo's "Record & Compare" mic widget.
export default function MicRecorder() {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [status, setStatus] = useState<"idle" | "recording">("idle");
  const [volume, setVolume] = useState(0);
  const [url, setUrl] = useState<string | null>(null);

  const streamRef = useRef<MediaStream | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number | null>(null);

  const stopVisual = () => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    setVolume(0);
    audioCtxRef.current?.close();
    audioCtxRef.current = null;
    analyserRef.current = null;
  };

  const stopAll = () => {
    if (recorderRef.current && recorderRef.current.state !== "inactive") {
      recorderRef.current.stop();
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    stopVisual();
    setStatus("idle");
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await navigator.mediaDevices.getUserMedia({ audio: true });
        const all = await navigator.mediaDevices.enumerateDevices();
        const inputs = all.filter((d) => d.kind === "audioinput");
        if (cancelled) return;
        setDevices(inputs);
        if (inputs.length) setDeviceId(inputs[0].deviceId);
      } catch {
        // mic unavailable or permission denied; leave dropdown empty
      }
    })();
    return () => {
      cancelled = true;
      stopAll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const draw = () => {
    const analyser = analyserRef.current;
    if (!analyser) return;
    const dataArray = new Uint8Array(analyser.fftSize);
    const loop = () => {
      rafRef.current = requestAnimationFrame(loop);
      analyser.getByteTimeDomainData(dataArray);
      let sum = 0;
      for (let i = 0; i < dataArray.length; i++) {
        const v = (dataArray[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / dataArray.length);
      setVolume(Math.min(100, Math.round(rms * 3 * 100)));
    };
    loop();
  };

  const start = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      streamRef.current = stream;

      const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const audioCtx = new Ctx();
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      audioCtxRef.current = audioCtx;
      analyserRef.current = analyser;
      draw();

      chunksRef.current = [];
      const recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: "audio/webm" });
        setUrl(URL.createObjectURL(blob));
      };
      recorder.start();
      recorderRef.current = recorder;
      setStatus("recording");
    } catch {
      setStatus("idle");
    }
  };

  const stop = () => stopAll();

  return (
    <div className="mic">
      <div className="mic-head">
        <span className="mic-title">Record &amp; Compare</span>
        <span className={`mic-pill ${status}`}>
          <span className="mic-dot" />
          {status === "recording" ? "Recording" : "Disconnected"}
        </span>
      </div>

      <select
        className="mic-select"
        value={deviceId}
        onChange={(e) => setDeviceId(e.target.value)}
      >
        {devices.length === 0 ? (
          <option value="">No microphone detected</option>
        ) : (
          devices.map((d, i) => (
            <option key={d.deviceId} value={d.deviceId}>
              {d.label || `Microphone ${i + 1}`}
            </option>
          ))
        )}
      </select>

      <div className="row" style={{ marginTop: 6 }}>
        <button className="primary" onClick={start} disabled={status === "recording"}>
          Start
        </button>
        <button onClick={stop} disabled={status !== "recording"}>
          Stop
        </button>
      </div>

      <div className="mic-meter">
        <div className="mic-meter-track">
          <div className="mic-meter-fill" style={{ width: `${volume}%` }} />
        </div>
        <div className="mic-vol muted">Vol: {volume}%</div>
      </div>

      {url && <audio controls src={url} className="mic-playback" />}
    </div>
  );
}
