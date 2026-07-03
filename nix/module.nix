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

      host = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "Host/address to bind the tracking server to.";
      };

      dbPath = lib.mkOption {
        type = lib.types.str;
        default = "/var/lib/jernerics/db.sqlite";
        description = "Path to the SQLite database file.";
      };

      httpPort = lib.mkOption {
        type = lib.types.port;
        default = 8000;
        description = "HTTP port for the tracking server.";
      };

      apiKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          File containing JERNERICS_API_KEY.
          Compatible with sops-nix EnvironmentFile format.
        '';
      };

      artifactsDir = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Directory to store artifact files. null uses a sibling of the database.";
      };
    };
  };

  config = {
    # --- tracking server ---
    systemd.services.jernerics-tracking = {
      description = "Jernerics HTTP tracking server";
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig =
        let
          artifactsFlag = lib.optionalString (
            cfg.tracking.artifactsDir != null
          ) " --artifacts-dir ${cfg.tracking.artifactsDir}";
        in
        {
          ExecStart = "${cfg.tracking.package}/bin/python -m jernerics_server --db ${cfg.tracking.dbPath} --host ${cfg.tracking.host} --http-port ${toString cfg.tracking.httpPort}${artifactsFlag}";
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
        } // lib.optionalAttrs (cfg.tracking.apiKeyFile != null) {
          EnvironmentFile = cfg.tracking.apiKeyFile;
        };
    };
  };
}
