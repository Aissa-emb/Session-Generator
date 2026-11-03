import { useRef } from "react";
import { Json } from "./api";

// A moving window of actual responses, with stable receipt keys. No simulated traffic.
export default function DeliveryActivity({
  receipts,
  active,
  now,
  backend,
}: {
  receipts: Json[];
  active: boolean;
  now: number;
  backend: boolean;
}) {
  const lastLive = useRef(
    active ? now : Math.min(now, (receipts[0]?.time_ms || now * 1000) / 1000),
  );
  if (active) lastLive.current = now;
  const end = lastLive.current * 1000;
  const recent = receipts.filter(
    (r) => r.time_ms <= end && r.time_ms > end - 60000,
  );
  return (
    <div
      className={"delivery-activity " + (active ? "is-active" : "")}
      role="img"
      aria-label={
        active ? "Live delivery activity" : "Delivery activity paused"
      }
      title="Each mark is a recorded destination response in the last 60 seconds."
    >
      <div className="activity-caption">
        <span>Delivery activity</span>
        <span>{active ? "Live" : "Last minute"}</span>
      </div>
      <div className="activity-lanes">
        {[
          { target: "embrace", label: "RUM" },
          ...(backend ? [{ target: "otlp", label: "Backend" }] : []),
        ].map((lane) => (
          <div className="activity-lane" key={lane.target}>
            <span className="activity-label">{lane.label}</span>
            <div className="activity-track">
              {recent
                .filter((r) => r.target === lane.target)
                .map((r) => (
                  <i
                    key={r.id}
                    className={
                      "activity-mark " +
                      (r.status === "accepted" ? "accepted" : "problem")
                    }
                    style={{
                      left: `${100 * (1 - (end - r.time_ms) / 60000)}%`,
                      height: `${Math.min(22, 5 + Math.log2(1 + (r.accepted || r.rejected || 0)) * 2)}px`,
                    }}
                  />
                ))}
            </div>
          </div>
        ))}
      </div>
      <div className="activity-axis">
        <span>−60s</span>
        <span>{active ? "now" : "held"}</span>
      </div>
    </div>
  );
}
