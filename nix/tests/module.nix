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
    import json

    machine.wait_for_unit("jernerics-tracking.service")
    machine.wait_for_open_port(8080)

    # HTTP query endpoint requires auth
    output = machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/query -H 'Content-Type: application/json' -d '{\"sql\": \"SELECT 1\"}'")
    assert "401" in output

    # HTTP query endpoint works with valid key
    output = machine.succeed("curl -s -X POST http://localhost:8080/query -H 'Content-Type: application/json' -H 'Authorization: Bearer test-secret-key' -d '{\"sql\": \"SELECT 1 AS n\"}'")
    assert "200" in machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/query -H 'Content-Type: application/json' -H 'Authorization: Bearer test-secret-key' -d '{\"sql\": \"SELECT 1 AS n\"}'")
    assert "\"n\"" in output

    # Artifact round-trip (v3): declare via /ingest, then PUT/GET by artifact id
    sha = machine.succeed("sha256sum /etc/hostname | cut -d' ' -f1").strip()
    size = int(machine.succeed("stat -Lc%s /etc/hostname").strip())
    artifact_id = "3f2a81c6b1d94e7f9a0c5d2e8f1a3b4c"
    trial_id = "1f5512e4-15aa-4083-8a15-8d60c4ae3703"
    sweep_id = "4e9c986d-63e3-4a4f-b430-5275ea431467"
    execution_id = "e6f2d9cc-2e22-4cd4-8953-b090f244de57"
    events = [
        {"event_id": "02a8e2a3-a044-4a0c-9bb4-554c669438a3", "recorded_at": "2026-01-01T00:00:00Z", "tag": "sweep_snapshot", "project": "proj", "sweep_id": sweep_id, "name": "vm-check", "state": "running"},
        {"event_id": "0bbe6fd4-a22e-4844-8d2c-1fdfb233fe22", "recorded_at": "2026-01-01T00:00:01Z", "tag": "trial_snapshot", "trial_id": trial_id, "sweep_id": sweep_id, "number": 0, "state": "running", "retry_root_trial_id": trial_id, "retry_index": 0, "params": {}, "objective": None, "distributions": None, "attrs": None, "retry_of_trial_id": None},
        {"event_id": "12862a48-3ac8-4f3c-a93d-5e88c4e90bf5", "recorded_at": "2026-01-01T00:00:02Z", "tag": "execution_start", "execution_id": execution_id, "trial_id": trial_id, "hostname": "node01", "host_facts": None, "started_at": "2026-01-01T00:00:02Z"},
        {"event_id": "79a79f08-bc48-4949-a20c-12b303eac623", "recorded_at": "2026-01-01T00:00:03Z", "tag": "artifact_declaration", "artifact_id": artifact_id, "trial_id": trial_id, "execution_id": execution_id, "key": "model", "filename": "hostname.bin", "content_type": "application/octet-stream", "size_bytes": size, "sha256": sha, "context": None, "source": "user"},
    ]
    machine.succeed("cat > /tmp/decl.json <<'EOF'\n" + json.dumps({"protocol_version": 3, "events": events}) + "\nEOF")
    code = machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8080/ingest -H 'Content-Type: application/json' -H 'Authorization: Bearer test-secret-key' --data-binary @/tmp/decl.json")
    assert "200" in code, f"ingest failed: {code}"
    code = machine.succeed("curl -s -o /dev/null -w '%{http_code}' -X PUT http://localhost:8080/artifact/" + artifact_id + " -H 'Authorization: Bearer test-secret-key' --data-binary @/etc/hostname")
    assert "200" in code, f"artifact PUT failed: {code}"
    machine.succeed("curl -s -o /tmp/got http://localhost:8080/artifact/" + artifact_id + " -H 'Authorization: Bearer test-secret-key'")
    machine.succeed("diff /etc/hostname /tmp/got")
  '';
}
