using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class BackgroundRepair
{
    private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false);

    private sealed class Result
    {
        public bool Success;
        public string Detail;
        public string LogPath;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--check")
        {
            try { using (var archive = Resource("release.zip")) VerifyArchive(archive); return 0; }
            catch { return 2; }
        }

        int port = 18766;
        string overridePort = Environment.GetEnvironmentVariable("MOVER_WORKBENCH_PORT");
        int configured;
        if (int.TryParse(overridePort, out configured) && configured >= 1024 && configured <= 65535) port = configured;
        if (args.Length == 1 && args[0] == "--headless") return Run(port).Success ? 0 : 1;

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        int exitCode = 1;
        using (var form = new Form())
        {
            form.Text = "搬运工V2 · 修复后台设置";
            form.ClientSize = new Size(462, 174);
            form.Font = SystemFonts.MessageBoxFont;
            form.FormBorderStyle = FormBorderStyle.FixedDialog;
            form.MaximizeBox = false;
            form.StartPosition = FormStartPosition.CenterScreen;
            var status = new Label { Bounds = new Rectangle(22, 20, 418, 81), Text = "正在检查原工作台。需要更新时会短暂重启服务，请等到验证完成。" };
            var open = new Button { Bounds = new Rectangle(22, 114, 122, 33), Enabled = false, Text = "打开本地设置" };
            var log = new Button { Bounds = new Rectangle(156, 114, 122, 33), Enabled = false, Text = "查看修复日志" };
            var close = new Button { Bounds = new Rectangle(318, 114, 122, 33), Enabled = false, Text = "关闭" };
            form.Controls.AddRange(new Control[] { status, open, log, close });
            var ready = false;
            form.FormClosing += delegate(object sender, FormClosingEventArgs e) { if (!ready && e.CloseReason == CloseReason.UserClosing) e.Cancel = true; };
            open.Click += delegate { OpenTarget("http://127.0.0.1:" + port + "/?page=settings"); };
            close.Click += delegate { form.Close(); };
            form.Shown += async delegate
            {
                var result = await Task.Run(() => Run(port));
                exitCode = result.Success ? 0 : 1;
                status.Text = result.Success
                    ? "后台设置已通过保存验证（" + result.Detail + "）。\r\n请打开本地设置，勾选你要使用的选项后保存。"
                    : result.Detail;
                open.Enabled = result.Success;
                if (File.Exists(result.LogPath))
                {
                    log.Enabled = true;
                    log.Click += delegate { OpenTarget(Path.Combine(Environment.SystemDirectory, "notepad.exe"), Quote(result.LogPath)); };
                }
                ready = true;
                close.Enabled = true;
            };
            Application.Run(form);
        }
        return exitCode;
    }

    private static Result Run(int port)
    {
        var result = new Result { Detail = "修复程序未能启动。请查看本次修复日志。" };
        string scratch = null;
        string temp = null;
        try
        {
            string logDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "InformationMoverV2", "RepairLogs");
            Directory.CreateDirectory(logDirectory);
            string runId = DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N");
            result.LogPath = Path.Combine(logDirectory, "background-" + runId + ".log");
            temp = Path.GetFullPath(Path.GetTempPath()).TrimEnd('\\') + "\\";
            scratch = Path.Combine(temp, "mover-bg-repair-" + runId);
            Directory.CreateDirectory(scratch);
            string archivePath = Path.Combine(scratch, "release.zip");
            string scriptPath = Path.Combine(scratch, "repair-background.ps1");
            string resultPath = Path.Combine(scratch, "result.txt");

            using (var archive = Resource("release.zip"))
            {
                VerifyArchive(archive);
                archive.Position = 0;
                using (var file = File.Create(archivePath)) archive.CopyTo(file);
            }
            // Windows PowerShell 5.1 needs the BOM to read localized script strings reliably.
            using (var script = new StreamReader(Resource("repair.ps1"), Utf8, true))
                File.WriteAllText(scriptPath, script.ReadToEnd(), new UTF8Encoding(true));
            File.WriteAllText(result.LogPath, "Embedded release checksum verified." + Environment.NewLine, Utf8);

            var start = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell", "v1.0", "powershell.exe"),
                Arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(scriptPath) +
                    " -ArchivePath " + Quote(archivePath) + " -LogPath " + Quote(result.LogPath) +
                    " -ResultPath " + Quote(resultPath) + " -Port " + port,
                WorkingDirectory = scratch,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            using (var process = Process.Start(start))
            {
                process.WaitForExit();
                if (File.Exists(resultPath))
                {
                    var detail = File.ReadAllText(resultPath, Utf8).Trim();
                    Version version;
                    result.Success = process.ExitCode == 0 && Version.TryParse(detail, out version);
                    if (result.Success || process.ExitCode != 0) result.Detail = detail;
                }
                else File.AppendAllText(result.LogPath, "No repair result; PowerShell exit code: " + process.ExitCode + Environment.NewLine, Utf8);
            }
        }
        catch (Exception error)
        {
            if (result.LogPath != null)
            {
                try { File.AppendAllText(result.LogPath, error.Message + Environment.NewLine, Utf8); }
                catch { }
            }
        }
        finally
        {
            if (scratch != null && Path.GetFullPath(scratch).StartsWith(temp + "mover-bg-repair-", StringComparison.OrdinalIgnoreCase))
            {
                try { Directory.Delete(scratch, true); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
        return result;
    }

    private static Stream Resource(string name)
    {
        var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
        if (resource == null) throw new InvalidDataException("Missing resource: " + name);
        return resource;
    }

    private static void VerifyArchive(Stream archive)
    {
        using (var sha = SHA256.Create())
        using (var expected = new StreamReader(Resource("release.sha256")))
        {
            string actual = BitConverter.ToString(sha.ComputeHash(archive)).Replace("-", "");
            if (!string.Equals(actual, expected.ReadToEnd().Trim(), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Repair archive checksum mismatch.");
        }
    }

    private static string Quote(string path) { return "\"" + path + "\""; }

    private static void OpenTarget(string fileName, string arguments = "")
    {
        try { Process.Start(new ProcessStartInfo(fileName, arguments) { UseShellExecute = true }); }
        catch (Exception error) { MessageBox.Show(error.Message, "搬运工V2", MessageBoxButtons.OK, MessageBoxIcon.Error); }
    }
}
