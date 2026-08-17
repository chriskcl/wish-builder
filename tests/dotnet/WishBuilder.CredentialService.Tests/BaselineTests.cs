using Microsoft.VisualStudio.TestTools.UnitTesting;

namespace WishBuilder.CredentialService.Tests;

[TestClass]
[DoNotParallelize]
public sealed class BaselineTests
{
    [TestMethod]
    public void VersionCommandReportsThePinnedProductVersion()
    {
        TextWriter originalOutput = Console.Out;
        using var output = new StringWriter();
        try
        {
            Console.SetOut(output);
            int exitCode = Program.Main(["--version"]);

            Assert.AreEqual(0, exitCode);
            Assert.AreEqual(
                "wish-builder-credential-service 0.1.0-dev",
                output.ToString().Trim()
            );
        }
        finally
        {
            Console.SetOut(originalOutput);
        }
    }

    [TestMethod]
    public void ServiceModeFailsClosedUntilItIsImplemented()
    {
        TextWriter originalError = Console.Error;
        using var error = new StringWriter();
        try
        {
            Console.SetError(error);
            int exitCode = Program.Main([]);

            Assert.AreEqual(78, exitCode);
            Assert.AreEqual(
                "SETUP_REQUIRED: the Windows credential service is not implemented yet.",
                error.ToString().Trim()
            );
        }
        finally
        {
            Console.SetError(originalError);
        }
    }
}
