using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;
using System.Threading;
using System.Security.Cryptography;

internal static class PortableLauncher
{
    private static readonly object LogLock = new object();

    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(root, "update-and-start.ps1");
        bool openWorkbench = Array.IndexOf(args, "--open") >= 0;
        bool resumeBackground = Array.IndexOf(args, "--resume-background") >= 0;
        bool completePayload = PayloadExists(root);
        if (args.Length > 0 && args[0] == "--check")
            return completePayload ? 0 : 2;

        if (!completePayload)
        {
            string message = "安装包内容不完整。请完整解压新版 ZIP。";
            if (openWorkbench) MessageBox.Show(message, "搬运工V2", MessageBoxButtons.OK, MessageBoxIcon.Error);
            else try
            {
                Directory.CreateDirectory(Path.Combine(root, "runtime", "logs"));
                File.AppendAllText(Path.Combine(root, "runtime", "logs", "launcher-errors.log"),
                    DateTime.UtcNow.ToString("o") + " " + message + Environment.NewLine, new UTF8Encoding(false));
            }
            catch { }
            return 2;
        }

        string logDirectory = Path.Combine(root, "runtime", "logs");
        string installKey = MutexName(root);
        string runLogPath = Path.Combine(logDirectory, "launcher-" + DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffZ") + "-" +
            Process.GetCurrentProcess().Id + "-" + Guid.NewGuid().ToString("N") + ".log");
        string logPath = runLogPath;
        Mutex bootstrapMutex = null;
        bool ownsMutex = false;
        try
        {
            Directory.CreateDirectory(logDirectory);
            bootstrapMutex = new Mutex(false, installKey);
            try { ownsMutex = bootstrapMutex.WaitOne(0); }
            catch (AbandonedMutexException) { ownsMutex = true; }
            if (!ownsMutex)
            {
                // A same-install bootstrap is already running. The running bootstrap
                // owns the state transition; return success without racing its files.
                File.AppendAllText(logPath, "同一安装目录已有启动流程，等待现有工作台。" + Environment.NewLine, new UTF8Encoding(false));
                return 0;
            }
            string powershell = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell", "v1.0", "powershell.exe");
            var start = new ProcessStartInfo
            {
                FileName = powershell,
                Arguments = "-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"" + script + "\"" +
                    (openWorkbench ? "" : " -NoBrowser -SkipDesktopActions") +
                    (resumeBackground ? " -ResumeBackground" : ""),
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            start.EnvironmentVariables["PATH"] = Path.Combine(root, "tools") + ";" +
                (Environment.GetEnvironmentVariable("PATH") ?? "");

            using (var logStream = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete))
            using (var log = new StreamWriter(logStream, new UTF8Encoding(false)))
            using (var process = new Process())
            {
                process.StartInfo = start;
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
                {
                    if (eventArgs.Data != null) WriteLog(log, eventArgs.Data);
                };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
                {
                    if (eventArgs.Data != null) WriteLog(log, eventArgs.Data);
                };
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                process.WaitForExit();
                if (process.ExitCode != 0)
                {
                    WriteLog(log, "Startup failed; exit code: " + process.ExitCode);
                    if (openWorkbench) MessageBox.Show(
                        "搬运工V2未能启动。请查看启动日志：" + logPath + "\n\n退出代码：" + process.ExitCode,
                        "搬运工V2", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return process.ExitCode;
                }
            }
            bootstrapMutex.ReleaseMutex();
            ownsMutex = false;
            bootstrapMutex.Dispose();
            bootstrapMutex = null;
            return 0;
        }
        catch (Exception exception)
        {
            try { File.AppendAllText(logPath, exception.ToString() + Environment.NewLine, new UTF8Encoding(false)); }
            catch { }
            if (openWorkbench) MessageBox.Show(
                "搬运工V2启动失败，请检查网络与启动日志：" + logPath + "\n\n" + exception.Message,
                "搬运工V2", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            if (bootstrapMutex != null)
            {
                if (ownsMutex) try { bootstrapMutex.ReleaseMutex(); } catch { }
                bootstrapMutex.Dispose();
            }
        }
    }

    private static bool PayloadExists(string root)
    {
        // --check only inspects local files. It must never bootstrap services,
        // mutate user data, register startup or attempt a download.
        string[] required = {
            "update-and-start.ps1", "updater.py", "start-workbench.ps1",
            "workbench_server.py", Path.Combine("astrbot_plugin_message_bridge", "metadata.yaml"),
            Path.Combine("tools", "uv.exe")
        };
        foreach (string relative in required)
            if (!File.Exists(Path.Combine(root, relative))) return false;
        return true;
    }

    private static string MutexName(string root)
    {
        string normalized = Path.GetFullPath(root).TrimEnd('\\', '/').ToUpperInvariant();
        using (var digest = SHA256.Create())
        {
            byte[] hash = digest.ComputeHash(Encoding.UTF8.GetBytes(normalized));
            return "Local\\MowerV2Bootstrap-" + BitConverter.ToString(hash).Replace("-", "");
        }
    }

    private static void WriteLog(StreamWriter log, string line)
    {
        lock (LogLock)
        {
            log.WriteLine(line);
            log.Flush();
        }
    }
}
