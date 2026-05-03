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
      services.jernerics.tracking.httpHost = "0.0.0.0";
      services.jernerics.tracking.apiKeyFile = pkgs.writeText "api-key" ''
        JERNERICS_API_KEY=test-secret-key
      '';
      services.jernerics.minio.dataDir = "/var/lib/minio/data";
      services.jernerics.minio.credentialsFile = pkgs.writeText "minio-creds" ''
        MINIO_ROOT_USER=admin
        MINIO_ROOT_PASSWORD=admin12345
      '';

      environment.systemPackages = [
        pkgs.minio-client
        pkgs.curl
      ];

      system.stateVersion = "25.05";
    };

  testScript = ''
    machine.wait_for_unit("minio.service")
    machine.wait_for_open_port(9000)

    machine.wait_for_unit("jernerics-tracking.service")
    machine.wait_for_open_port(50051)
    machine.wait_for_open_port(8080)

    machine.wait_for_unit("jernerics-bucket-setup.service")

    # Verify bucket exists
    machine.succeed("mc alias set local http://localhost:9000 admin admin12345")
    machine.succeed("mc ls local/jernerics")

    # HTTP query endpoint requires auth
    output = machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/query -H 'Content-Type: application/json' -d '{\"sql\": \"SELECT 1\"}'")
    assert "401" in output

    # HTTP query endpoint works with valid key
    output = machine.succeed("curl -s -X POST http://localhost:8080/query -H 'Content-Type: application/json' -H 'Authorization: Bearer test-secret-key' -d '{\"sql\": \"SELECT 1 AS n\"}'")
    assert "200" in machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/query -H 'Content-Type: application/json' -H 'Authorization: Bearer test-secret-key' -d '{\"sql\": \"SELECT 1 AS n\"}'")
    assert "\"n\"" in output
  '';
}
