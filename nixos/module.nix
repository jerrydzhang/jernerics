{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.jernerics;
in
{
  options.services.jernerics = {
    tracking = {
      package = lib.mkOption {
        type = lib.types.package;
        description = "jernerics-server virtualenv package from the flake.";
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 50051;
        description = "gRPC port for the tracking server.";
      };

      host = lib.mkOption {
        type = lib.types.str;
        default = "[::]";
        description = "Host/address to bind the tracking server to.";
      };

      dbPath = lib.mkOption {
        type = lib.types.path;
        default = "/var/lib/jernerics/db.duckdb";
        description = "Path to the DuckDB database file.";
      };
    };

    minio = {
      port = lib.mkOption {
        type = lib.types.port;
        default = 9000;
        description = "Port for the minIO S3 API.";
      };

      consolePort = lib.mkOption {
        type = lib.types.port;
        default = 9001;
        description = "Port for the minIO web console.";
      };

      bucket = lib.mkOption {
        type = lib.types.str;
        default = "jernerics";
        description = "Bucket name to auto-provision on first start.";
      };

      dataDir = lib.mkOption {
        type = lib.types.path;
        description = "Data directory for minIO object storage.";
      };

      credentialsFile = lib.mkOption {
        type = lib.types.path;
        description = ''
          File containing MINIO_ROOT_USER and MINIO_ROOT_PASSWORD.
          Compatible with sops-nix EnvironmentFile format.
        '';
      };
    };
  };

  config = {
    # --- minIO ---
    services.minio = {
      enable = true;
      listenAddress = ":${toString cfg.minio.port}";
      consoleAddress = ":${toString cfg.minio.consolePort}";
      dataDir = [ cfg.minio.dataDir ];
      rootCredentialsFile = cfg.minio.credentialsFile;
    };

    # --- tracking server ---
    systemd.services.jernerics-tracking = {
      description = "Jernerics gRPC tracking server";
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        ExecStart = "${cfg.tracking.package}/bin/python -m jernerics_server --db ${cfg.tracking.dbPath} --host ${cfg.tracking.host} --port ${toString cfg.tracking.port}";
        Type = "simple";
        Restart = "on-failure";
        RestartSec = 5;

        DynamicUser = true;
        StateDirectory = "jernerics";
        ProtectHome = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictNamespaces = true;
        MemoryDenyWriteExecute = false;
        LockPersonality = true;
      };
    };

    # --- bucket auto-provisioning ---
    systemd.services.jernerics-bucket-setup = {
      description = "Provision minIO bucket for jernerics";
      wants = [ "minio.service" ];
      after = [ "minio.service" ];
      wantedBy = [ "multi-user.target" ];

      path = [
        pkgs.minio-client
        pkgs.glibc.getent
      ];

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        EnvironmentFile = cfg.minio.credentialsFile;
      };

      script = ''
        # Wait for minIO to accept connections
        for i in $(seq 1 30); do
          if ${pkgs.minio-client}/bin/mc alias set local http://localhost:${toString cfg.minio.port} "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" 2>/dev/null; then
            break
          fi
          echo "Waiting for minIO... ($i/30)"
          sleep 1
        done

        # Create bucket if it doesn't exist
        ${pkgs.minio-client}/bin/mc mb --ignore-existing local/${cfg.minio.bucket}
        echo "Bucket '${cfg.minio.bucket}' ready."
      '';
    };
  };
}
