pragma Singleton
import QtQuick

QtObject {
    // ---- Surfaces -------------------------------------------------------
    readonly property color base:      "#07080b"
    readonly property color surface:   "#0e1016"
    readonly property color surfaceHi: "#151922"
    readonly property color hairline:  "#1e2330"

    // ---- Accents --------------------------------------------------------
    readonly property color cyan:   "#4cd6ff"
    readonly property color violet: "#b66bff"
    readonly property color red:    "#ff5e7e"
    readonly property color orange: "#ff9a4d"
    readonly property color green:  "#4dffb4"
    readonly property color yellow: "#ffd84d"

    // ---- Text -----------------------------------------------------------
    readonly property color textHi:  "#eef1f7"
    readonly property color textMid: "#8b93a7"
    readonly property color textLo:  "#4d5566"

    // ---- Type -----------------------------------------------------------
    // SF Pro is not redistributable on Linux; the fallback chain degrades
    // to Inter, then Roboto, then the platform sans.
    readonly property string display: "SF Pro Display, Inter, Roboto, sans-serif"
    readonly property string text:    "SF Pro Text, Inter, Roboto, sans-serif"
    readonly property string mono:    "SF Mono, JetBrains Mono, monospace"

    // ---- Rhythm ---------------------------------------------------------
    readonly property int u: 8          // base spacing unit
    readonly property int radius: 14

    // ---- State -> accent mapping ----------------------------------------
    function accentFor(state) {
        switch (state) {
        case "listening": return cyan
        case "thinking":  return violet
        case "speaking":  return green
        case "error":     return red
        default:          return "#2a3242"   // idle: near-surface, barely lit
        }
    }

    function labelFor(state) {
        switch (state) {
        case "listening": return "Listening"
        case "thinking":  return "Thinking"
        case "speaking":  return "Speaking"
        case "error":     return "Error"
        default:          return "Idle"
        }
    }
}
