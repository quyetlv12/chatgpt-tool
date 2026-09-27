import AppKit
import Darwin
import Foundation

private func argumentValue(after flag: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: flag) else { return nil }
    let valueIndex = CommandLine.arguments.index(after: index)
    guard valueIndex < CommandLine.arguments.endIndex else { return nil }
    return CommandLine.arguments[valueIndex]
}

private final class MenuBarDelegate: NSObject, NSApplicationDelegate {
    private let parentPID: pid_t
    private let toolURL: URL
    private var statusItem: NSStatusItem?
    private var parentMonitor: Timer?

    init(parentPID: pid_t, toolURL: URL) {
        self.parentPID = parentPID
        self.toolURL = toolURL
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            if let image = NSImage(systemSymbolName: "shield.checkered", accessibilityDescription: "Shoptaikhoan Tool") {
                image.isTemplate = true
                button.image = image
            } else {
                button.title = "ST"
            }
            button.toolTip = "Shoptaikhoan Tool đang chạy"
        }

        let menu = NSMenu()
        let heading = NSMenuItem(title: "Shoptaikhoan Tool", action: nil, keyEquivalent: "")
        heading.isEnabled = false
        menu.addItem(heading)

        let status = NSMenuItem(title: "Đang chạy tại 127.0.0.1:5033", action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)
        menu.addItem(.separator())

        let openItem = NSMenuItem(title: "Mở Tool", action: #selector(openTool(_:)), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)

        let quitItem = NSMenuItem(
            title: "Thoát Shoptaikhoan Tool",
            action: #selector(quitTool(_:)),
            keyEquivalent: "q"
        )
        quitItem.target = self
        menu.addItem(quitItem)

        item.menu = menu
        statusItem = item
        parentMonitor = Timer.scheduledTimer(
            timeInterval: 1,
            target: self,
            selector: #selector(checkParent(_:)),
            userInfo: nil,
            repeats: true
        )
    }

    func applicationWillTerminate(_ notification: Notification) {
        parentMonitor?.invalidate()
    }

    @objc private func openTool(_ sender: Any?) {
        NSWorkspace.shared.open(toolURL)
    }

    @objc private func quitTool(_ sender: Any?) {
        Darwin.kill(parentPID, SIGTERM)
        NSApp.terminate(nil)
    }

    @objc private func checkParent(_ timer: Timer) {
        errno = 0
        if Darwin.kill(parentPID, 0) != 0 && errno == ESRCH {
            NSApp.terminate(nil)
        }
    }
}

@main
private struct MenuBarApplication {
    static func main() {
        guard
            let pidValue = argumentValue(after: "--parent-pid"),
            let parentPID = pid_t(pidValue),
            parentPID > 1,
            let urlValue = argumentValue(after: "--url"),
            let toolURL = URL(string: urlValue),
            toolURL.scheme == "http",
            ["127.0.0.1", "localhost", "::1"].contains(toolURL.host ?? "")
        else {
            FileHandle.standardError.write(Data("Invalid menu-bar arguments\n".utf8))
            exit(2)
        }

        let application = NSApplication.shared
        let delegate = MenuBarDelegate(parentPID: parentPID, toolURL: toolURL)
        application.delegate = delegate
        application.run()
    }
}
