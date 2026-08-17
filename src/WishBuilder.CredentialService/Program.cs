namespace WishBuilder.CredentialService;

internal static class Program
{
    public static int Main(string[] args)
    {
        if (args is ["--version"])
        {
            Console.WriteLine(
                $"wish-builder-credential-service {ServiceBuildInfo.ProductVersion}"
            );
            return 0;
        }

        Console.Error.WriteLine(
            "SETUP_REQUIRED: the Windows credential service is not implemented yet."
        );
        return ServiceBuildInfo.SetupRequiredExitCode;
    }
}
