using System.Text;
using System.Text.RegularExpressions;

namespace CadBridge;

internal static partial class MTextProtector
{
    [GeneratedRegex(@"⟦MT_\d{4}⟧", RegexOptions.CultureInvariant)]
    private static partial Regex TokenPattern();

    // Lower-case \p...; carries paragraph properties (indent, line spacing, etc.).
    // Upper-case \P remains the two-character paragraph break.
    private static readonly HashSet<char> TerminatedCodes = new("ACFHQRSTWXacfhpqrstwx");

    public static (string Text, ProtectedSequence[] Sequences) Protect(string contents)
    {
        if (TokenPattern().IsMatch(contents))
            throw new InvalidDataException("MText contains a reserved CadBridge placeholder.");

        var output = new StringBuilder(contents.Length);
        var sequences = new List<ProtectedSequence>();
        var index = 0;
        while (index < contents.Length)
        {
            var length = SequenceLength(contents, index);
            if (length == 0)
            {
                output.Append(contents[index++]);
                continue;
            }

            var token = $"⟦MT_{sequences.Count + 1:0000}⟧";
            var value = contents.Substring(index, length);
            sequences.Add(new ProtectedSequence(token, value));
            output.Append(token);
            index += length;
        }
        return (output.ToString(), sequences.ToArray());
    }

    public static string Restore(string translated, IReadOnlyList<ProtectedSequence> sequences)
    {
        Validate(translated, sequences);
        var restored = translated;
        foreach (var sequence in sequences)
            restored = restored.Replace(sequence.Token, sequence.Value, StringComparison.Ordinal);
        if (TokenPattern().IsMatch(restored))
            throw new InvalidDataException("Unexpected MText placeholder remains after restoration.");
        return restored;
    }

    public static void Validate(string translated, IReadOnlyList<ProtectedSequence> sequences)
    {
        var expected = sequences.Select(item => item.Token).ToArray();
        if (expected.Distinct(StringComparer.Ordinal).Count() != expected.Length)
            throw new InvalidDataException("Duplicate MText placeholder token.");
        var actual = TokenPattern().Matches(translated).Select(match => match.Value).ToArray();
        if (!actual.SequenceEqual(expected, StringComparer.Ordinal) ||
            expected.Any(token => CountOrdinal(translated, token) != 1))
            throw new InvalidDataException("MText placeholders were changed, duplicated, removed, or added.");
    }

    private static int SequenceLength(string text, int index)
    {
        var current = text[index];
        if (current is '{' or '}')
            return 1;
        if (current != '\\' || index + 1 >= text.Length)
            return 0;

        var code = text[index + 1];
        if (TerminatedCodes.Contains(code))
        {
            var terminator = text.IndexOf(';', index + 2);
            return terminator < 0 ? text.Length - index : terminator - index + 1;
        }
        return 2;
    }

    private static int CountOrdinal(string text, string value)
    {
        var count = 0;
        var start = 0;
        while ((start = text.IndexOf(value, start, StringComparison.Ordinal)) >= 0)
        {
            count++;
            start += value.Length;
        }
        return count;
    }
}
