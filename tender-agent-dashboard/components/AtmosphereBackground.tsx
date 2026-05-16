// Decorative fixed-position background layer — eight blueprint SVGs plus two
// animated blue light trails. Imported once in app/layout.tsx so it sits
// behind every route. SVG paths and animation timings are verbatim from
// docs/mockups/genera-tenders-mockup-v2.html.

const blueprintTextStyle = {
  fontFamily: "'Courier New', monospace",
  fontSize: "11px",
  fill: "rgba(160, 200, 255, 0.22)",
};

export default function AtmosphereBackground() {
  return (
    <>
      {/* Layer 1: blueprint SVGs */}
      <div
        aria-hidden="true"
        className="blueprint-bg pointer-events-none fixed inset-0 z-0 mix-screen"
        style={{ opacity: 0.55 }}
      >
        {/* Top-left wireframe building isometric */}
        <svg
          viewBox="0 0 200 160"
          className="absolute"
          style={{
            top: "8%",
            left: "3%",
            width: "180px",
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <path d="M30 70 L100 30 L170 70 L100 110 Z" />
          <path d="M30 70 L30 130 L100 170 L100 110" />
          <path d="M170 70 L170 130 L100 170" />
          <path d="M55 55 L125 95 M85 35 L155 75 M55 85 L55 145 M85 65 L85 125 M115 105 L115 165 M145 85 L145 145" />
          <text x="10" y="155" style={{ ...blueprintTextStyle, fontSize: "10px" }}>
            L1: 28.4m
          </text>
          <text x="60" y="20" style={{ ...blueprintTextStyle, fontSize: "9px" }}>
            [ wireframe ]
          </text>
        </svg>

        {/* Equation block */}
        <svg
          viewBox="0 0 280 80"
          className="absolute"
          style={{
            top: "12%",
            left: "38%",
            width: "260px",
            opacity: 0.5,
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <text x="0" y="20" style={{ ...blueprintTextStyle, fontSize: "14px" }}>
            P(σ) = ∫ φ(x) dx — λ²
          </text>
          <text x="0" y="42" style={blueprintTextStyle}>
            Σ ⟨ψᵢ | H | ψⱼ⟩ = E·δᵢⱼ
          </text>
          <text x="0" y="62" style={blueprintTextStyle}>
            ∂u/∂t + (u·∇)u = -∇p + ν∇²u
          </text>
        </svg>

        {/* Top-right architectural elevation — hidden under lg */}
        <svg
          viewBox="0 0 220 160"
          className="absolute hidden lg:block"
          style={{
            top: "6%",
            right: "4%",
            width: "200px",
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <rect x="20" y="40" width="180" height="100" />
          <line x1="20" y1="40" x2="200" y2="40" />
          <line x1="40" y1="40" x2="40" y2="140" />
          <line x1="80" y1="40" x2="80" y2="140" />
          <line x1="120" y1="40" x2="120" y2="140" />
          <line x1="160" y1="40" x2="160" y2="140" />
          <line x1="20" y1="70" x2="200" y2="70" />
          <line x1="20" y1="100" x2="200" y2="100" />
          <line x1="20" y1="130" x2="200" y2="130" />
          <path d="M20 40 L110 15 L200 40" />
          <text x="0" y="155" style={{ ...blueprintTextStyle, fontSize: "9px" }}>
            elevation • south
          </text>
        </svg>

        {/* Gear (mid-left) */}
        <svg
          viewBox="0 0 140 140"
          className="absolute"
          style={{
            top: "45%",
            left: "2%",
            width: "130px",
            opacity: 0.4,
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <circle cx="70" cy="70" r="45" />
          <circle cx="70" cy="70" r="20" />
          <circle cx="70" cy="70" r="8" />
          <line x1="70" y1="10" x2="70" y2="30" />
          <line x1="70" y1="110" x2="70" y2="130" />
          <line x1="10" y1="70" x2="30" y2="70" />
          <line x1="110" y1="70" x2="130" y2="70" />
          <line x1="28" y1="28" x2="42" y2="42" />
          <line x1="98" y1="98" x2="112" y2="112" />
          <line x1="112" y1="28" x2="98" y2="42" />
          <line x1="42" y1="98" x2="28" y2="112" />
        </svg>

        {/* Skyline (mid-right) — hidden under lg */}
        <svg
          viewBox="0 0 260 140"
          className="absolute hidden lg:block"
          style={{
            top: "38%",
            right: "3%",
            width: "240px",
            opacity: 0.35,
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <line x1="0" y1="130" x2="260" y2="130" />
          <rect x="10" y="80" width="20" height="50" />
          <rect x="35" y="50" width="25" height="80" />
          <rect x="65" y="20" width="30" height="110" />
          <rect x="100" y="60" width="20" height="70" />
          <rect x="125" y="35" width="35" height="95" />
          <rect x="165" y="70" width="22" height="60" />
          <rect x="192" y="45" width="28" height="85" />
          <rect x="225" y="85" width="25" height="45" />
          <text x="0" y="120" style={{ ...blueprintTextStyle, fontSize: "8px" }}>
            UK • skyline
          </text>
        </svg>

        {/* Chart (bottom-middle) */}
        <svg
          viewBox="0 0 300 120"
          className="absolute"
          style={{
            bottom: "12%",
            left: "25%",
            width: "280px",
            opacity: 0.3,
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <line x1="0" y1="100" x2="300" y2="100" />
          <line x1="0" y1="0" x2="0" y2="100" />
          <path d="M0 80 L40 65 L80 70 L120 45 L160 55 L200 30 L240 35 L280 15" />
          <circle cx="40" cy="65" r="2" />
          <circle cx="80" cy="70" r="2" />
          <circle cx="120" cy="45" r="2" />
          <circle cx="160" cy="55" r="2" />
          <circle cx="200" cy="30" r="2" />
          <circle cx="240" cy="35" r="2" />
          <text x="0" y="115" style={{ ...blueprintTextStyle, fontSize: "9px" }}>
            pipeline volume • 2026 ytd
          </text>
        </svg>

        {/* Bar graph (bottom-right) */}
        <svg
          viewBox="0 0 240 140"
          className="absolute"
          style={{
            bottom: "8%",
            right: "6%",
            width: "220px",
            opacity: 0.4,
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <line x1="0" y1="120" x2="240" y2="120" />
          <rect x="10" y="80" width="20" height="40" />
          <rect x="40" y="60" width="20" height="60" />
          <rect x="70" y="40" width="20" height="80" />
          <rect x="100" y="50" width="20" height="70" />
          <rect x="130" y="25" width="20" height="95" />
          <rect x="160" y="35" width="20" height="85" />
          <rect x="190" y="15" width="20" height="105" />
          <text x="0" y="135" style={{ ...blueprintTextStyle, fontSize: "9px" }}>
            cpv 45 • monthly
          </text>
        </svg>

        {/* Lower-left equation */}
        <svg
          viewBox="0 0 220 60"
          className="absolute"
          style={{
            bottom: "28%",
            left: "8%",
            width: "200px",
            opacity: 0.45,
            stroke: "rgba(160, 200, 255, 0.18)",
            strokeWidth: 1,
            fill: "none",
          }}
        >
          <text x="0" y="18" style={{ ...blueprintTextStyle, fontSize: "12px" }}>
            w(t) = A·sin(ωt + φ)
          </text>
          <text x="0" y="38" style={blueprintTextStyle}>
            ∇ × E = -∂B/∂t
          </text>
          <text x="0" y="56" style={{ ...blueprintTextStyle, fontSize: "10px" }}>
            ROI = (V − C) / C × 100%
          </text>
        </svg>
      </div>

      {/* Layer 2: blue light trail */}
      <div
        aria-hidden="true"
        className="pointer-events-none fixed inset-0 z-[1] overflow-hidden"
      >
        <svg
          viewBox="0 0 1920 1080"
          preserveAspectRatio="xMidYMid slice"
          className="absolute inset-0 h-full w-full"
        >
          <path
            d="M -100 200 Q 400 100 700 400 T 1300 600 Q 1600 700 2020 500"
            pathLength={2200}
            fill="none"
            stroke="rgba(110, 180, 255, 0.5)"
            strokeWidth={1.5}
            style={{
              strokeDasharray: "0 2200",
              strokeDashoffset: 0,
              filter:
                "drop-shadow(0 0 4px rgba(110, 180, 255, 0.7)) drop-shadow(0 0 12px rgba(110, 180, 255, 0.3))",
              animation: "trail-flow 14s ease-in-out infinite",
            }}
          />
          <path
            d="M -100 700 Q 300 600 600 800 T 1100 750 Q 1500 700 2020 850"
            pathLength={2400}
            fill="none"
            stroke="rgba(110, 180, 255, 0.35)"
            strokeWidth={1}
            style={{
              strokeDasharray: "0 2400",
              filter: "drop-shadow(0 0 3px rgba(110, 180, 255, 0.5))",
              animation: "trail-flow-2 18s ease-in-out infinite 3s",
            }}
          />
          <circle
            r={3}
            cx={0}
            cy={0}
            fill="rgba(160, 210, 255, 0.9)"
            style={{
              offsetPath:
                "path('M -100 200 Q 400 100 700 400 T 1300 600 Q 1600 700 2020 500')",
              filter:
                "drop-shadow(0 0 6px rgba(160, 210, 255, 0.9)) drop-shadow(0 0 14px rgba(110, 180, 255, 0.5))",
              animation: "trail-dot 14s ease-in-out infinite",
            }}
          />
        </svg>
      </div>
    </>
  );
}
