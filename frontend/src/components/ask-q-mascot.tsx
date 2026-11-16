"use client";

import { useId } from "react";

/** Code-native SVG: the face, eyes, smile, hands and float are individually animated. */
export function AskQMascot({ thinking = false, className = "" }: { thinking?: boolean; className?: string }) {
  const id = useId().replaceAll(":", "");
  return <svg className={`ask-q-mascot ${thinking ? "thinking" : ""} ${className}`} viewBox="0 0 144 144" fill="none" aria-hidden="true" focusable="false">
    <defs>
      <linearGradient id={`${id}-shell`} x1="37" y1="29" x2="111" y2="107" gradientUnits="userSpaceOnUse"><stop stopColor="#FCFFFE" /><stop offset=".58" stopColor="#DDF7EB" /><stop offset="1" stopColor="#98DCC5" /></linearGradient>
      <linearGradient id={`${id}-face`} x1="47" y1="41" x2="101" y2="84" gradientUnits="userSpaceOnUse"><stop stopColor="#173F3A" /><stop offset="1" stopColor="#076551" /></linearGradient>
      <linearGradient id={`${id}-body`} x1="59" y1="89" x2="88" y2="122" gradientUnits="userSpaceOnUse"><stop stopColor="#F7FFFC" /><stop offset="1" stopColor="#B4E7D5" /></linearGradient>
      <filter id={`${id}-shadow`} x="18" y="12" width="108" height="126" filterUnits="userSpaceOnUse"><feDropShadow dx="0" dy="5" stdDeviation="4" floodColor="#0F6855" floodOpacity=".13" /></filter>
    </defs>
    <ellipse className="ask-q-shadow" cx="73" cy="131" rx="31" ry="5" fill="#0E7359" opacity=".12" />
    <g className="ask-q-sparkles" stroke="#54AC91" strokeWidth="1.8" strokeLinecap="round"><path d="M118 31v8m-4-4h8" /><path d="M22 48v6m-3-3h6" /><circle cx="117" cy="101" r="2.4" fill="#C7ECDE" stroke="none" /></g>
    <g className="ask-q-float">
      <g className="ask-q-arm ask-q-arm-left"><path d="M49 93C37 96 29 89 25 78" stroke="#2C9A7D" strokeWidth="8" strokeLinecap="round" /><path d="M49 92C38 95 30 88 27 79" stroke="#A7E0CB" strokeWidth="4" strokeLinecap="round" /><path d="m25 80-6-5m7 4-1-10m2 11 5-7" stroke="#E8FFF5" strokeWidth="5" strokeLinecap="round" /></g>
      <g className="ask-q-arm ask-q-arm-right"><path d="M98 93c11-2 16-10 17-20" stroke="#2C9A7D" strokeWidth="8" strokeLinecap="round" /><path d="M98 92c10-2 14-10 15-19" stroke="#A7E0CB" strokeWidth="4" strokeLinecap="round" /><path d="m115 74-5-7m5 7 1-11m0 12 6-5" stroke="#E8FFF5" strokeWidth="5" strokeLinecap="round" /></g>
      <g filter={`url(#${id}-shadow)`}>
        <path d="M60 110v10m25-10v10" stroke="#25896D" strokeWidth="9" strokeLinecap="round" />
        <path d="M57 122h8m17 0h8" stroke="#0C7058" strokeWidth="7" strokeLinecap="round" />
        <rect x="49" y="84" width="48" height="34" rx="17" fill={`url(#${id}-body)`} stroke="#70BDA3" strokeWidth="1.5" />
        <circle cx="73" cy="103" r="8" fill="#0B8267" /><path d="M76 105.5 78 108m-2.5-7.5a3.7 3.7 0 1 0-5.2 5.2 3.7 3.7 0 0 0 5.2-5.2Z" stroke="#E5FFF4" strokeWidth="1.7" strokeLinecap="round" />
        <path d="M73 29v-9" stroke="#54A88D" strokeWidth="3" strokeLinecap="round" /><circle className="ask-q-antenna" cx="73" cy="17" r="5" fill="#80D7B8" stroke="#E3FFF3" strokeWidth="2" />
        <rect x="34" y="28" width="78" height="66" rx="29" fill={`url(#${id}-shell)`} stroke="#69B99E" strokeWidth="1.5" />
        <path d="M45 43c4-7 11-10 19-10" stroke="white" strokeWidth="3" strokeLinecap="round" opacity=".85" />
        <rect x="43" y="40" width="60" height="42" rx="19" fill={`url(#${id}-face)`} stroke="#1D7962" strokeWidth="1.2" />
        <path d="M54 45c7-3 25-4 35 0" stroke="#69B49D" strokeWidth="1.3" strokeLinecap="round" opacity=".32" />
        <g className="ask-q-eyes"><ellipse cx="60" cy="57" rx="4.5" ry="6" fill="#D9FFF0" /><ellipse cx="86" cy="57" rx="4.5" ry="6" fill="#D9FFF0" /><circle cx="61.5" cy="55" r="1.4" fill="white" /><circle cx="87.5" cy="55" r="1.4" fill="white" /></g>
        <path className="ask-q-smile" d="M65 69c4.5 5 11.5 5 16 0" stroke="#C5FCE4" strokeWidth="2.7" strokeLinecap="round" />
        <ellipse cx="52" cy="67" rx="4" ry="2" fill="#5AD9AA" opacity=".35" /><ellipse cx="94" cy="67" rx="4" ry="2" fill="#5AD9AA" opacity=".35" />
      </g>
    </g>
  </svg>;
}
