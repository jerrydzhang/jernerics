{
  self,
  system,
  pkgs,
}:

pkgs.testers.nixosTest {
  name = "jernerics";

  nodes.machine =
    { pkgs, ... }:
    {
      imports = [ self.nixosModules.default ];

      services.jernerics.tracking.package = self.packages.${system}.jernerics-server;
      services.jernerics.tracking.httpPort = 8080;
      services.jernerics.tracking.host = "0.0.0.0";
      services.jernerics.tracking.artifactsDir = "/var/lib/jernerics/artifacts";
      services.jernerics.tracking.apiKeyFile = pkgs.writeText "api-key" ''
        JERNERICS_API_KEY=test-secret-key
      '';

      environment.systemPackages = [ pkgs.curl ];

      system.stateVersion = "25.05";
    };

  testScript = ''
    machine.wait_for_unit("jernerics-tracking.service")
    machine.wait_for_open_port(8080)

    # HTTP query endpoint requires auth
    output = machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/query -H 'Content-Type: application/json' -d '{\"sql\": \"SELECT 1\"}'")
    assert "401" in output

    # HTTP query endpoint works with valid key
    output = machine.succeed("curl -s -X POST http://localhost:8080/query -H 'Content-Type: application/json' -H 'Authorization: Bearer test-secret-key' -d '{\"sql\": \"SELECT 1 AS n\"}'")
    assert "200" in machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/query -H 'Content-Type: application/json' -H 'Authorization: Bearer test-secret-key' -d '{\"sql\": \"SELECT 1 AS n\"}'")
    assert "\"n\"" in output

    # Artifact upload + download round-trip
    machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/artifact/proj/study/0/ckpt -H 'Authorization: Bearer test-secret-key' -F 'file=@/etc/hostname'")
    machine.succeed("curl -s -o /tmp/got -w '' http://localhost:8080/artifact/proj/study/0/ckpt -H 'Authorization: Bearer test-secret-key'")
    machine.succeed("diff /etc/hostname /tmp/got")
  '';
}
