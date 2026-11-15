using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class PortableLauncher
{
    private static readonly object LogLock = new object();

    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(root, "start-workbench.ps1");
        string uv = Path.Combine(root, "tools", "uv.exe");
        if (args.Length > 0 && args[0] == "--check")
            return File.Exists(script) && File.Exists(uv) ? 0 : 2;

        if (!File.Exists(script) || !File.Exists(uv))
        {
            MessageBox.Show(
                "安装包内容不完整。请将 ZIP 中的所有文件解压到同一个文件夹后，再运行搬运工V2.exe。",
                "搬运工V2", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }

        string logDirectory = Path.Combine(root, "runtime", "logs");
        string logPath = Path.Combine(logDirectory, "launcher.log");
        try
        {
            Directory.CreateDirectory(logDirectory);
            string powershell = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell", "v1.0", "powershell.exe");
            var start = new ProcessStartInfo
            {
                FileName = powershell,
                Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            start.EnvironmentVariables["PATH"] = Path.Combine(root, "tools") + ";" +
                (Environment.GetEnvironmentVariable("PATH") ?? "");

            using (var log = new StreamWriter(logPath, true, new UTF8Encoding(false)))
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
                    MessageBox.Show(
                        "搬运工V2未能启动。请查看 runtime\\logs\\launcher.log 和 AstrBot 日志。\n\n退出代码：" + process.ExitCode,
                        "搬运工V2", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return process.ExitCode;
                }
            }
            return 0;
        }
        catch (Exception exception)
        {
            try { File.AppendAllText(logPath, exception.ToString() + Environment.NewLine, new UTF8Encoding(false)); }
            catch { }
            MessageBox.Show(
                "搬运工V2启动失败，请检查网络和 runtime\\logs\\launcher.log。\n\n" + exception.Message,
                "搬运工V2", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
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
