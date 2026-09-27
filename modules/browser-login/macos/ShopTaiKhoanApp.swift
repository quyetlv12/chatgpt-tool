import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let dashboardURL = URL(string: "http://localhost:9876/")!
    private var serverProcess: Process?
    private var ownsServer = false
    private var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureStatusMenu()
        ensureServerAndOpenDashboard()
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard ownsServer else { return }
        stopAutomationSessions()
        serverProcess?.terminate()
        serverProcess?.waitUntilExit()
    }

    private func configureStatusMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "ST"
        item.button?.toolTip = "shoptaikhoan auto tool"

        let menu = NSMenu()
        let openItem = NSMenuItem(title: "Mở giao diện", action: #selector(openDashboard), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)

        let stopItem = NSMenuItem(title: "Hủy các phiên Chrome", action: #selector(stopSessions), keyEquivalent: "")
        stopItem.target = self
        menu.addItem(stopItem)
        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Thoát shoptaikhoan auto tool", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        item.menu = menu
        statusItem = item
    }

    private func ensureServerAndOpenDashboard() {
        checkHealth { [weak self] isHealthy in
            guard let self else { return }
            if isHealthy {
                DispatchQueue.main.async { self.openDashboard() }
                return
            }
            DispatchQueue.main.async {
                do {
                    try self.startServer()
                    self.waitForServer(attempt: 0)
                } catch {
                    self.showLaunchError(error.localizedDescription)
                }
            }
        }
    }

    private func startServer() throws {
        guard
            let resources = Bundle.main.resourceURL,
            let serverURL = Bundle.main.url(forResource: "shoptaikhoan-server", withExtension: nil, subdirectory: "bin"),
            let workerURL = Bundle.main.url(forResource: "shoptaikhoan-auto-login", withExtension: nil, subdirectory: "bin")
        else {
            throw NSError(domain: "ShopTaiKhoanApp", code: 1, userInfo: [NSLocalizedDescriptionKey: "Không tìm thấy thành phần chạy của ứng dụng."])
        }

        let supportURL = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ).appendingPathComponent("shoptaikhoan-auto-tool", isDirectory: true)
        try FileManager.default.createDirectory(at: supportURL, withIntermediateDirectories: true)

        let logURL = supportURL.appendingPathComponent("app.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let logHandle = try FileHandle(forWritingTo: logURL)
        try logHandle.seekToEnd()

        var environment = ProcessInfo.processInfo.environment
        environment["SHOPTAIKHOAN_NO_OPEN_BROWSER"] = "1"
        environment["SHOPTAIKHOAN_DATA_DIR"] = supportURL.path
        environment["SHOPTAIKHOAN_AUTO_LOGIN_EXECUTABLE"] = workerURL.path
        let bundledBrowsers = resources.appendingPathComponent("ms-playwright", isDirectory: true)
        if FileManager.default.fileExists(atPath: bundledBrowsers.path) {
            environment["PLAYWRIGHT_BROWSERS_PATH"] = bundledBrowsers.path
        }

        let process = Process()
        process.executableURL = serverURL
        process.currentDirectoryURL = resources
        process.environment = environment
        process.standardOutput = logHandle
        process.standardError = logHandle
        try process.run()
        serverProcess = process
        ownsServer = true
    }

    private func waitForServer(attempt: Int) {
        guard attempt < 40 else {
            showLaunchError("Server cục bộ không khởi động sau 10 giây. Xem log tại ~/Library/Application Support/shoptaikhoan-auto-tool/app.log")
            return
        }
        checkHealth { [weak self] ready in
            guard let self else { return }
            if ready {
                DispatchQueue.main.async { self.openDashboard() }
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                    self.waitForServer(attempt: attempt + 1)
                }
            }
        }
    }

    private func checkHealth(completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: dashboardURL.appendingPathComponent("api/health"))
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200 && data != nil
            completion(ok)
        }.resume()
    }

    private func stopAutomationSessions() {
        let endpoints = ["api/web-login/stop", "api/oauth/auto-stop"]
        for endpoint in endpoints {
            var request = URLRequest(url: dashboardURL.appendingPathComponent(endpoint))
            request.httpMethod = "POST"
            request.timeoutInterval = 1
            let semaphore = DispatchSemaphore(value: 0)
            URLSession.shared.dataTask(with: request) { _, _, _ in semaphore.signal() }.resume()
            _ = semaphore.wait(timeout: .now() + 1.5)
        }
    }

    private func showLaunchError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Không thể mở shoptaikhoan auto tool"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.runModal()
    }

    @objc private func openDashboard() {
        NSWorkspace.shared.open(dashboardURL)
    }

    @objc private func stopSessions() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.stopAutomationSessions()
        }
    }

    @objc private func quitApp() {
        NSApplication.shared.terminate(nil)
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
