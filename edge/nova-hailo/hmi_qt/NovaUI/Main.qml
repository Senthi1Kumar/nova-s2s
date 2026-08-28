import QtQuick
import QtQuick.Window
import QtQuick.Controls
import NovaUI

ApplicationWindow {
    id: win
    width: 1024
    height: 600
    visible: true
    title: "NOVA HMI"
    color: Theme.base

    readonly property bool kiosk: Qt.application.arguments.indexOf("--kiosk") >= 0
    visibility: kiosk ? Window.FullScreen : Window.Windowed
    flags: kiosk ? (Qt.FramelessWindowHint | Qt.Window) : Qt.Window

    Shell { anchors.fill: parent }

    Shortcut {
        sequence: "Escape"
        onActivated: {
            if (nova.settingsOpen || nova.chatOpen || nova.opsOpen)
                nova.closeDrawers()
            else
                Qt.quit()
        }
    }
}
