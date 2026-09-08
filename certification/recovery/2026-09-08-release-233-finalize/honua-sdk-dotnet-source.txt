// Copyright (c) Honua. All rights reserved.
// Licensed under the Apache License, Version 2.0. See LICENSE in the project root.

using Honua.Sdk.Admin.Models;

namespace Honua.Sdk.Admin;

/// <summary>
/// Compatibility helpers and baselines for the Honua Admin SDK.
/// </summary>
public static class HonuaAdminCompatibility
{
    /// <summary>
    /// Minimum Honua Server version supported by this SDK baseline.
    /// </summary>
    public const string MinimumSupportedServerVersion = "0.1.0";

    /// <summary>
    /// Control-plane API major version expected by this SDK.
    /// </summary>
    public const int SupportedControlPlaneApiMajor = 1;

    /// <summary>
    /// Control-plane API base path expected by this SDK.
    /// </summary>
    public const string SupportedControlPlaneApiBasePath = "/api/v1/admin";

    /// <summary>
    /// Minimum server release channel expected by this SDK baseline.
    /// </summary>
    public const string MinimumSupportedReleaseChannel = "preview";

    /// <summary>
    /// Evaluates whether a server capabilities payload is supported by this SDK.
    /// </summary>
    /// <param name="capabilities">Capabilities payload returned by the server.</param>
    /// <returns>A compatibility result with support status and coarse feature metadata.</returns>
    public static ServerCompatibilityResult Evaluate(AdminCapabilitiesResponse capabilities)
    {
        ArgumentNullException.ThrowIfNull(capabilities);

        if (string.IsNullOrWhiteSpace(capabilities.ServerVersion))
        {
            return Unsupported(capabilities, "Server did not return compatibility metadata.");
        }

        if (!SemanticVersion.TryParse(capabilities.ServerVersion, out var serverVersion))
        {
            return Unsupported(
                capabilities,
                $"Server version '{capabilities.ServerVersion}' could not be parsed.");
        }

        if (!SemanticVersion.TryParse(MinimumSupportedServerVersion, out var minimumVersion))
        {
            throw new InvalidOperationException(
                $"Configured minimum supported server version '{MinimumSupportedServerVersion}' is invalid.");
        }

        if (serverVersion.CompareTo(minimumVersion) < 0)
        {
            return Unsupported(
                capabilities,
                $"Server version '{capabilities.ServerVersion}' is below the minimum supported server version '{MinimumSupportedServerVersion}'.");
        }

        if (capabilities.ControlPlaneApi.Major != SupportedControlPlaneApiMajor)
        {
            return Unsupported(
                capabilities,
                $"Control-plane API major '{capabilities.ControlPlaneApi.Major}' is not supported. Expected '{SupportedControlPlaneApiMajor}'.");
        }

        var normalizedBasePath = NormalizePath(capabilities.ControlPlaneApi.BasePath);
        if (!string.Equals(normalizedBasePath, SupportedControlPlaneApiBasePath, StringComparison.OrdinalIgnoreCase))
        {
            return Unsupported(
                capabilities,
                $"Control-plane API base path '{capabilities.ControlPlaneApi.BasePath}' is not supported. Expected '{SupportedControlPlaneApiBasePath}'.");
        }

        if (capabilities.ControlPlaneApi.Deprecated)
        {
            return Unsupported(
                capabilities,
                "The advertised control-plane API major is deprecated.");
        }

        var actualReleaseChannelRank = GetReleaseChannelRank(capabilities.ReleaseChannel);
        var minimumReleaseChannelRank = GetReleaseChannelRank(MinimumSupportedReleaseChannel);
        if (actualReleaseChannelRank < 0)
        {
            return Unsupported(
                capabilities,
                $"Release channel '{capabilities.ReleaseChannel}' is not recognized by this SDK baseline.");
        }

        if (actualReleaseChannelRank < minimumReleaseChannelRank)
        {
            return Unsupported(
                capabilities,
                $"Release channel '{capabilities.ReleaseChannel}' is below the minimum supported release channel '{MinimumSupportedReleaseChannel}'.");
        }

        return new ServerCompatibilityResult
        {
            Capabilities = capabilities,
            IsSupported = true
        };
    }

    private static ServerCompatibilityResult Unsupported(AdminCapabilitiesResponse capabilities, string reason)
        => new()
        {
            Capabilities = capabilities,
            IsSupported = false,
            UnsupportedReason = reason
        };

    private static string NormalizePath(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return string.Empty;
        }

        var normalized = path.Trim();
        if (!normalized.StartsWith('/'))
        {
            normalized = "/" + normalized;
        }

        return normalized.TrimEnd('/');
    }

    private static int GetReleaseChannelRank(string releaseChannel)
        => releaseChannel.Trim().ToUpperInvariant() switch
        {
            "NIGHTLY" => 0,
            "DEV" => 1,
            "ALPHA" => 2,
            "PREVIEW" => 3,
            "BETA" => 4,
            "RC" => 5,
            "STABLE" => 6,
            "LTS" => 7,
            _ => -1
        };

    private readonly record struct SemanticVersion(int Major, int Minor, int Patch) : IComparable<SemanticVersion>
    {
        public int CompareTo(SemanticVersion other)
        {
            var major = Major.CompareTo(other.Major);
            if (major != 0)
            {
                return major;
            }

            var minor = Minor.CompareTo(other.Minor);
            if (minor != 0)
            {
                return minor;
            }

            return Patch.CompareTo(other.Patch);
        }

        public static bool TryParse(string? value, out SemanticVersion version)
        {
            version = default;

            if (string.IsNullOrWhiteSpace(value))
            {
                return false;
            }

            var normalized = value.Trim();
            if (normalized.StartsWith('v') || normalized.StartsWith('V'))
            {
                normalized = normalized[1..];
            }

            var preReleaseIndex = normalized.IndexOf('-', StringComparison.Ordinal);
            var buildIndex = normalized.IndexOf('+', StringComparison.Ordinal);
            var endIndex = normalized.Length;

            if (preReleaseIndex >= 0)
            {
                endIndex = Math.Min(endIndex, preReleaseIndex);
            }

            if (buildIndex >= 0)
            {
                endIndex = Math.Min(endIndex, buildIndex);
            }

            normalized = normalized[..endIndex];
            var parts = normalized.Split('.');
            if (parts.Length is < 2 or > 3)
            {
                return false;
            }

            if (!int.TryParse(parts[0], out var major) ||
                !int.TryParse(parts[1], out var minor))
            {
                return false;
            }

            var patch = 0;
            if (parts.Length == 3 && !int.TryParse(parts[2], out patch))
            {
                return false;
            }

            version = new SemanticVersion(major, minor, patch);
            return true;
        }
    }
}
