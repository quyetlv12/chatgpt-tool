import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let dashboardURL = URL(string: "http://127.0.0.1:5050/")!
    private var hubProcess: Process?
    private var ownsHub = false
    private var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let iconURL = Bundle.main.url(forResource: "MenuBarTemplate", withExtension: "svg"),
           let icon = NSImage(contentsOf: iconURL) {
            icon.isTemplate = true
            icon.size = NSSize(width: 19, height: 19)
            item.button?.image = icon
        } else {
            item.button?.title = "ST"
        }
        item.button?.toolTip = "Shoptaikhoan Suite"
        let menu = NSMenu()
        let open = NSMenuItem(title: "Mở giao diện", action: #selector(openDashboard), keyEquivalent: "o")
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Thoát Shoptaikhoan Suite", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        item.menu = menu
        statusItem = item
        ensureHub()
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard ownsHub, let process = hubProcess, process.isRunning else { return }
        var request = URLRequest(url: dashboardURL.appendingPathComponent("api/shutdown"))
        request.httpMethod = "POST"
        request.timeoutInterval = 2
        let semaphore = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, _, _ in semaphore.signal() }.resume()
        _ = semaphore.wait(timeout: .now() + 2)
        for _ in 0..<30 where process.isRunning { Thread.sleep(forTimeInterval: 0.1) }
        if process.isRunning { process.terminate() }
        process.waitUntilExit()
    }

    private func ensureHub() {
        checkHealth { [weak self] ready in
            guard let self else { return }
            DispatchQueue.main.async {
                if ready { self.openDashboard(); return }
                do { try self.startHub(); self.waitForHub(attempt: 0) }
                catch { self.showError(error.localizedDescription) }
            }
        }
    }

    private func startHub() throws {
        guard let resources = Bundle.main.resourceURL,
              let hub = Bundle.main.url(forResource: "shoptaikhoan-suite-hub", withExtension: nil, subdirectory: "bin")
        else { throw NSError(domain: "ShoptaikhoanSuite", code: 1, userInfo: [NSLocalizedDescriptionKey: "Thiếu thành phần chạy của Suite."]) }

        let engines = resources.appendingPathComponent("engines", isDirectory: true)
        let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("Shoptaikhoan Suite", isDirectory: true)
        try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        let logURL = support.appendingPathComponent("suite.log")
        if !FileManager.default.fileExists(atPath: logURL.path) { FileManager.default.createFile(atPath: logURL.path, contents: nil) }
        let log = try FileHandle(forWritingTo: logURL)
        try log.seekToEnd()

        var env = ProcessInfo.processInfo.environment
        env["SHOPTAIKHOAN_SUITE_DATA_DIR"] = support.appendingPathComponent("runtime", isDirectory: true).path
        env["SHOPTAIKHOAN_SUITE_TWOFA_EXECUTABLE"] = engines.appendingPathComponent("TwoFA.app/Contents/MacOS/Shoptaikhoan Tool").path
        env["SHOPTAIKHOAN_SUITE_EXPORT_EXECUTABLE"] = engines.appendingPathComponent("GPT-Tool.app/Contents/MacOS/GPT-Tool").path
        env["SHOPTAIKHOAN_SUITE_BROWSER_EXECUTABLE"] = engines.appendingPathComponent("BrowserLogin.app/Contents/Resources/bin/shoptaikhoan-server").path
        env["SHOPTAIKHOAN_AUTO_LOGIN_EXECUTABLE"] = engines.appendingPathComponent("BrowserLogin.app/Contents/Resources/bin/shoptaikhoan-auto-login").path
        env["PLAYWRIGHT_BROWSERS_PATH"] = engines.appendingPathComponent("BrowserLogin.app/Contents/Resources/ms-playwright", isDirectory: true).path
        env["SHOPTAIKHOAN_NO_MENU_BAR"] = "1"

        let process = Process()
        process.executableURL = hub
        process.arguments = ["--no-browser"]
        process.currentDirectoryURL = resources
        process.environment = env
        process.standardOutput = log
        process.standardError = log
        try process.run()
        hubProcess = process
        ownsHub = true
    }

    private func waitForHub(attempt: Int) {
        guard attempt < 80 else { showError("Suite không khởi động sau 20 giây. Xem log tại ~/Library/Application Support/Shoptaikhoan Suite/suite.log"); return }
        checkHealth { [weak self] ready in
            guard let self else { return }
            DispatchQueue.main.async {
                if ready { self.openDashboard() }
                else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForHub(attempt: attempt + 1) } }
            }
        }
    }

    private func checkHealth(completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: dashboardURL.appendingPathComponent("api/status"))
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { data, response, _ in
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let data,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let modules = payload["modules"] as? [[String: Any]],
                  !modules.isEmpty
            else { completion(false); return }
            completion(modules.allSatisfy { $0["ready"] as? Bool == true })
        }.resume()
    }

    private func showError(_ message: String) {
        let alert = NSAlert(); alert.messageText = "Không thể mở Shoptaikhoan Suite"; alert.informativeText = message; alert.alertStyle = .critical; alert.runModal()
    }

    @objc private func openDashboard() { NSWorkspace.shared.open(dashboardURL) }
    @objc private func quitApp() { NSApplication.shared.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
