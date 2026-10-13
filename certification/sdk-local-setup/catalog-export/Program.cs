// This offline adapter consumes an exact-source server test build. It never
// starts a server or uses an HTTP endpoint. Reflection deliberately fails closed
// if the reviewed source composition/projection seams change.
using System.Collections;
using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;

if (args.Length != 2)
    throw new ArgumentException("Expected the exact-source Honua.Ai.Tests.dll and an output file.");
var assemblyPath = Path.GetFullPath(args[0]);
var resolver = new AssemblyDependencyResolver(assemblyPath);
AssemblyLoadContext.Default.Resolving += (context, name) =>
{
    var path = resolver.ResolveAssemblyToPath(name);
    return path is null ? null : context.LoadFromAssemblyPath(path);
};
var tests = AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
if (tests.GetName().Name != "Honua.Ai.Tests")
    throw new InvalidOperationException("Expected the canonical server test assembly.");
var ai = AssemblyLoadContext.Default.LoadFromAssemblyName(new AssemblyName("Honua.Ai"));
const BindingFlags Static = BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
const BindingFlags Instance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
Type AiType(string name) => ai.GetType("Honua.Ai.Protocols.Mcp." + name, throwOnError: true)!;
object Get(object target, string name) => target.GetType().GetProperty(name, Instance)?.GetValue(target)
    ?? throw new InvalidOperationException("Missing source property: " + name);
object Invoke(Type type, string name, params object[] parameters) =>
    type.GetMethod(name, Static)?.Invoke(null, parameters)
    ?? throw new InvalidOperationException("Missing source method: " + name);
var workflowTests = tests.GetType("Honua.Server.Tests.Features.Protocols.Mcp.McpWorkflowViewTests", true)!;
var task = (Task)Invoke(workflowTests, "LiveCatalogAsync");
await task;
var catalog = Get(task, "Result");
var definition = AiType("Views.McpWorkflowViewCatalog").GetProperty("Setup", Static)?.GetValue(null)
    ?? throw new InvalidOperationException("Missing setup definition.");
var projector = AiType("Views.McpWorkflowViewProjector");
var projection = Invoke(projector, "Project", definition, catalog);
var staticProjection = Invoke(projector, "Project", definition, Invoke(workflowTests, "BuildCatalogEntries"));
foreach (var name in new[] { "RevisionDigest", "MembershipDigest", "DescriptorDigest" })
    if (!Equals(Get(projection, name), Get(staticProjection, name)))
        throw new InvalidOperationException("Setup now depends on dynamic composition; review the offline profile: " + name);
if (((IEnumerable)Get(projection, "BudgetViolations")).Cast<object>().Any()
    || ((IEnumerable)Get(projection, "EmptyStageIds")).Cast<object>().Any())
    throw new InvalidOperationException("Source setup projection has missing stages or budget violations.");

var context = AiType("Models.McpJsonContext").GetProperty("Default", Static)?.GetValue(null)
    ?? throw new InvalidOperationException("Missing canonical JSON context.");
string Wire(object value, string metadata) => JsonSerializer.Serialize(value, (JsonTypeInfo)Get(context, metadata));
var descriptorJson = Wire(Get(projection, "Descriptors"), "IReadOnlyListMcpToolDescriptor");
var serverInfo = Activator.CreateInstance(AiType("Models.McpServerInfo"))!;
var serverInfoJson = Wire(serverInfo, "McpServerInfo");
var members = ((IEnumerable)Get(projection, "Members")).Cast<object>().ToArray();
var membershipCanonical = string.Concat(members.Select(member =>
    Get(member, "StageId") + "/" + Get(member, "ToolName") + "\n"));
var revisionCanonical = definition.GetType().GetMethod("ToCanonicalString", Instance)?.Invoke(definition, null) as string
    ?? throw new InvalidOperationException("Missing canonical definition serialization.");
var stages = ((IEnumerable)Get(definition, "Stages")).Cast<object>()
    .Select(stage => (string)Get(stage, "Id")).ToArray();
var output = new
{
    schemaVersion = "honua.setup-source-projection/v1",
    view = Get(definition, "Name"),
    revision = Get(definition, "Revision"),
    revisionDigest = Get(projection, "RevisionDigest"),
    membershipDigest = Get(projection, "MembershipDigest"),
    descriptorDigest = Get(projection, "DescriptorDigest"),
    revisionCanonical,
    membershipCanonical,
    descriptorJson,
    descriptorJsons = members.Select(member => Wire(Get(member, "Descriptor"), "McpToolDescriptor")),
    serverInfo = JsonDocument.Parse(serverInfoJson).RootElement,
    tools = JsonDocument.Parse(descriptorJson).RootElement,
    stages,
    members = members.Select(member => new
    {
        name = Get(member, "ToolName"),
        stage = Get(member, "StageId"),
        isDynamic = Get(member, "IsDynamic")
    }),
    budgets = new
    {
        maxDescriptors = Get(definition, "MaxDescriptors"),
        maxAggregateBytes = Get(definition, "MaxAggregateDescriptorBytes"),
        maxDescriptorBytes = Get(definition, "MaxDescriptorBytes")
    },
    descriptorBytes = Get(projection, "AggregateCanonicalBytes"),
    largestDescriptorBytes = Get(projection, "LargestDescriptorBytes")
};
await File.WriteAllTextAsync(args[1], JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true }) + "\n");
