namespace WishBuilder.CredentialService;

internal static class Program
{
    private const int SetupRequiredExitCode = 78;

    public static int Main(string[] args)
    {
        if (args is ["--version"])
        {
            Console.WriteLine("wish-builder-credential-service 0.1.0-dev");
            return 0;
        }

        Console.Error.WriteLine(
            "SETUP_REQUIRED: the Windows credential service is not implemented yet."
        );
        return SetupRequiredExitCode;
    }
}
