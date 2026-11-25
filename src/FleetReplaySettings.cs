using System;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

internal sealed class FleetReplaySettings : Form
{
    private readonly string flag = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "full-fleet.enabled");
    private readonly CheckBox playerView = new CheckBox();
    private readonly Label description = new Label();
    private bool refreshing;

    private FleetReplaySettings()
    {
        Text = "OffHangar 리플레이 설정";
        Font = new Font("Malgun Gothic", 10);
        AutoScaleMode = AutoScaleMode.Font;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new Size(466, 172);
        playerView.Text = "플레이어에게 표시된 전차만 녹화";
        playerView.SetBounds(20, 17, 420, 32);
        description.SetBounds(39, 57, 408, 65);
        Button close = new Button { Text = "닫기", DialogResult = DialogResult.OK };
        close.SetBounds(348, 128, 96, 30);
        CancelButton = close;
        Controls.AddRange(new Control[] { playerView, description, close });
        playerView.CheckedChanged += Save;
        Activated += delegate { RefreshValue(); };
        RefreshValue();
    }

    private void RefreshValue()
    {
        refreshing = true;
        playerView.Checked = !File.Exists(flag);
        description.Text = playerView.Checked
            ? "다음 전투부터 내 전차와 표시 가능한 전차만 기록합니다. 재생 중 좌클릭으로 카메라를 전환할 수 있습니다."
            : "다음 전투부터 전체 전차를 기록합니다. 재생할 때 자유 카메라를 사용할 수 있습니다.";
        refreshing = false;
    }

    private void Save(object sender, EventArgs e)
    {
        if (refreshing) return;
        try
        {
            if (playerView.Checked) File.Delete(flag);
            else using (File.Open(flag, FileMode.OpenOrCreate, FileAccess.Write, FileShare.Read)) { }
        }
        catch (IOException ex) { MessageBox.Show(this, ex.Message, Text); }
        catch (UnauthorizedAccessException ex) { MessageBox.Show(this, ex.Message, Text); }
        RefreshValue();
    }

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new FleetReplaySettings());
    }
}
