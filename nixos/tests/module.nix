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
      services.jernerics.minio.dataDir = "/var/lib/minio/data";
      services.jernerics.minio.credentialsFile = pkgs.writeText "minio-creds" ''
        MINIO_ROOT_USER=admin
        MINIO_ROOT_PASSWORD=admin12345
      '';

      environment.systemPackages = [
        pkgs.minio-client
      ];

      system.stateVersion = "25.05";
    };

  testScript = ''
    machine.wait_for_unit("minio.service")
    machine.wait_for_open_port(9000)

    machine.wait_for_unit("jernerics-tracking.service")
    machine.wait_for_open_port(50051)

    machine.wait_for_unit("jernerics-bucket-setup.service")

    # Verify bucket exists
    machine.succeed("mc alias set local http://localhost:9000 admin admin12345")
    machine.succeed("mc ls local/jernerics")
  '';
}
