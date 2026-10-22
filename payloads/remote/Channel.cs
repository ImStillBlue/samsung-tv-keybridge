// HMAC framing adapted from samsung-tv-root at 4ad2c4d3b0d0,
// src/samsung_tv_root/root_agent.py and SamsungTvRemoteInputAgent.cs (Unlicense).
using System;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Theatre.Remote
{
    internal sealed class Configuration : IDisposable
    {
        public IPAddress Host { get; }
        public int Port { get; }
        public byte[] Token { get; }
        public string Mode { get; }

        private Configuration(IPAddress host, int port, byte[] token, string mode)
        {
            Host = host;
            Port = port;
            Token = token;
            Mode = mode;
        }

        // The staging owner must use a root-private directory and a root-owned
        // regular 0600 file. Only its absolute pathname goes on the command line.
        public static Configuration Consume(string path)
        {
            if (!Path.IsPathRooted(path))
                throw new InvalidDataException("configuration path must be absolute");
            byte[] bytes = Array.Empty<byte>();
            try
            {
                using (var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.None))
                {
                    if (file.Length < 1 || file.Length > 4096)
                        throw new InvalidDataException("invalid configuration length");
                    bytes = new byte[(int)file.Length];
                    int offset = 0;
                    while (offset < bytes.Length)
                    {
                        int read = file.Read(bytes, offset, bytes.Length - offset);
                        if (read == 0) throw new EndOfStreamException();
                        offset += read;
                    }
                    if (file.ReadByte() != -1)
                        throw new InvalidDataException("configuration changed during read");
                }
                // Failure to remove the secret prevents any input session.
                File.Delete(path);
                return Parse(bytes);
            }
            finally
            {
                Array.Clear(bytes, 0, bytes.Length);
            }
        }

        public static Configuration Parse(byte[] bytes)
        {
            using (JsonDocument document = JsonDocument.Parse(bytes))
            {
                JsonElement root = document.RootElement;
                if (root.ValueKind != JsonValueKind.Object)
                    throw new InvalidDataException("configuration must be an object");
                var seen = new System.Collections.Generic.HashSet<string>(StringComparer.Ordinal);
                foreach (JsonProperty property in root.EnumerateObject())
                {
                    if (!seen.Add(property.Name)
                        || (property.Name != "host" && property.Name != "port"
                            && property.Name != "token" && property.Name != "mode"))
                        throw new InvalidDataException("unknown or duplicate configuration field");
                }
                if (seen.Count != 4)
                    throw new InvalidDataException("missing configuration field");
                string? hostText = root.GetProperty("host").GetString();
                if (!IPAddress.TryParse(hostText, out IPAddress? host)
                    || host.AddressFamily != AddressFamily.InterNetwork
                    || host.ToString() != hostText)
                    throw new InvalidDataException("host must be a canonical IPv4 literal");
                if (!root.GetProperty("port").TryGetInt32(out int port) || port < 1 || port > 65535)
                    throw new InvalidDataException("invalid port");
                string? mode = root.GetProperty("mode").GetString();
                if (mode != "observe" && mode != "forward" && mode != "map")
                    throw new InvalidDataException("invalid mode");
                byte[] token = FrameCodec.DecodeHex(root.GetProperty("token").GetString() ?? "", 32);
                return new Configuration(host, port, token, mode);
            }
        }

        public void Dispose() => Array.Clear(Token, 0, Token.Length);
    }

    internal interface IClock
    {
        long Milliseconds { get; }
    }

    internal sealed class MonotonicClock : IClock
    {
        // CLOCK_BOOTTIME includes suspend; a TV waking with an absent host
        // must not resume a stale eight-second input lease.
        public long Milliseconds => NativeIo.BootMilliseconds();
    }

    internal sealed class Health
    {
        private readonly IClock clock;
        private long heartbeatDeadline;
        private long hardDeadline = long.MaxValue;
        public long LastSequence { get; private set; }
        public long Deadline => Math.Min(heartbeatDeadline, hardDeadline);

        public Health(IClock clock)
        {
            this.clock = clock;
            heartbeatDeadline = checked(clock.Milliseconds + 8000);
        }

        public void StartForwardWindow()
        {
            Check();
            hardDeadline = checked(clock.Milliseconds + 30000);
        }

        public void Accept(string payload)
        {
            Check();
            const string prefix = "HEARTBEAT\t";
            if (!payload.StartsWith(prefix, StringComparison.Ordinal))
                throw new InvalidDataException("only heartbeat frames are accepted");
            string text = payload.Substring(prefix.Length);
            if (!long.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out long sequence)
                || sequence <= 0 || sequence.ToString(CultureInfo.InvariantCulture) != text
                || (LastSequence == 0 ? sequence != 1 : sequence <= LastSequence))
                throw new InvalidDataException("invalid or replayed heartbeat sequence");
            // Expiry is terminal: even a valid frame cannot resurrect a stale grab.
            Check();
            LastSequence = sequence;
            heartbeatDeadline = checked(clock.Milliseconds + 8000);
        }

        public void Check() => Remaining(Deadline);

        public int Remaining(long operationDeadline)
        {
            long remaining = Math.Min(Deadline, operationDeadline) - clock.Milliseconds;
            if (remaining <= 0) throw new TimeoutException("input health deadline expired");
            return (int)Math.Min(remaining, int.MaxValue);
        }
    }

    internal static class FrameCodec
    {
        private static readonly Encoding Utf8 = new UTF8Encoding(false, true);
        public const int MaximumLineBytes = 4096;

        public static string Hex(byte[] bytes)
        {
            var result = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes) result.Append(value.ToString("x2", CultureInfo.InvariantCulture));
            return result.ToString();
        }

        public static byte[] DecodeHex(string text, int length)
        {
            if (text.Length != length * 2)
                throw new InvalidDataException("invalid hexadecimal length");
            var bytes = new byte[length];
            for (int index = 0; index < text.Length; index++)
            {
                char c = text[index];
                int nibble = c >= '0' && c <= '9' ? c - '0'
                    : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
                if (nibble < 0) throw new InvalidDataException("invalid lowercase hexadecimal");
                bytes[index / 2] |= (byte)(nibble << (index % 2 == 0 ? 4 : 0));
            }
            return bytes;
        }

        private static byte[] Authenticate(byte[] token, byte[] nonce, string payload)
        {
            if (token.Length != 32 || nonce.Length != 32)
                throw new InvalidDataException("invalid authentication material");
            byte[] data = Utf8.GetBytes(payload);
            var message = new byte[nonce.Length + 1 + data.Length];
            Buffer.BlockCopy(nonce, 0, message, 0, nonce.Length);
            Buffer.BlockCopy(data, 0, message, nonce.Length + 1, data.Length);
            using (var hmac = new HMACSHA256(token)) return hmac.ComputeHash(message);
        }

        public static byte[] Encode(byte[] token, byte[] nonce, string payload)
        {
            if (payload.IndexOfAny(new[] { '\r', '\n' }) >= 0)
                throw new InvalidDataException("embedded frame delimiter");
            byte[] bytes = Utf8.GetBytes(payload + "\t" + Hex(Authenticate(token, nonce, payload)) + "\n");
            if (bytes.Length > MaximumLineBytes)
                throw new InvalidDataException("frame exceeds size limit");
            return bytes;
        }

        public static string Verify(byte[] token, byte[] nonce, byte[] line)
        {
            if (line.Length == 0 || line.Length > MaximumLineBytes || line[line.Length - 1] != 10)
                throw new InvalidDataException("incomplete or oversized frame");
            string text = Utf8.GetString(line).TrimEnd('\r', '\n');
            int split = text.LastIndexOf('\t');
            if (split < 0) throw new InvalidDataException("missing authenticator");
            string payload = text.Substring(0, split);
            if (payload.IndexOfAny(new[] { '\r', '\n' }) >= 0)
                throw new InvalidDataException("embedded frame delimiter");
            byte[] observed = DecodeHex(text.Substring(split + 1), 32);
            if (!CryptographicOperations.FixedTimeEquals(observed, Authenticate(token, nonce, payload)))
                throw new InvalidDataException("frame authentication failed");
            return payload;
        }

        public static byte[] Challenge(byte[] line)
        {
            const string prefix = "THEATRE-REMOTE/1\t";
            string text = Utf8.GetString(line);
            if (!text.StartsWith(prefix, StringComparison.Ordinal)
                || text.Length != prefix.Length + 65 || text[text.Length - 1] != '\n')
                throw new InvalidDataException("invalid challenge");
            return DecodeHex(text.Substring(prefix.Length, 64), 32);
        }
    }

    // Read/Write are nonblocking: -1 means would-block, zero Read means EOF.
    internal interface ITransport : IDisposable
    {
        int Descriptor { get; }
        int Read(byte[] buffer, int offset, int count);
        int Write(byte[] buffer, int offset, int count);
        void Wait(bool writing, int milliseconds);
    }

    internal sealed class SocketTransport : ITransport
    {
        private readonly Socket socket;
        public int Descriptor => socket.Handle.ToInt32();

        private SocketTransport(Socket socket) { this.socket = socket; }

        public static SocketTransport Connect(Configuration config, Health health)
        {
            var socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
            try
            {
                socket.Blocking = false;
                socket.NoDelay = true;
                try { socket.Connect(new IPEndPoint(config.Host, config.Port)); }
                catch (SocketException error) when (error.SocketErrorCode == SocketError.WouldBlock
                    || error.SocketErrorCode == SocketError.InProgress)
                {
                    long deadline = health.Deadline;
                    while (!socket.Poll(checked(health.Remaining(deadline) * 1000), SelectMode.SelectWrite))
                        health.Check();
                    if (!(socket.GetSocketOption(SocketOptionLevel.Socket, SocketOptionName.Error) is int socketError)
                        || socketError != 0)
                        throw new IOException("callback connection failed");
                }
                health.Check();
                return new SocketTransport(socket);
            }
            catch { socket.Dispose(); throw; }
        }

        public int Read(byte[] buffer, int offset, int count)
        {
            try { return socket.Receive(buffer, offset, count, SocketFlags.None); }
            catch (SocketException error) when (error.SocketErrorCode == SocketError.WouldBlock
                || error.SocketErrorCode == SocketError.Interrupted) { return -1; }
        }

        public int Write(byte[] buffer, int offset, int count)
        {
            try { return socket.Send(buffer, offset, count, SocketFlags.None); }
            catch (SocketException error) when (error.SocketErrorCode == SocketError.WouldBlock
                || error.SocketErrorCode == SocketError.Interrupted) { return -1; }
        }

        public void Wait(bool writing, int milliseconds) =>
            socket.Poll(checked(milliseconds * 1000), writing ? SelectMode.SelectWrite : SelectMode.SelectRead);

        public void Dispose() => socket.Dispose();
    }

    internal sealed class AuthenticatedChannel : IDisposable
    {
        private readonly ITransport transport;
        private readonly Health health;
        private readonly byte[] token;
        private byte[] nonce = Array.Empty<byte>();
        private readonly byte[] incoming = new byte[1024];
        private readonly byte[] line = new byte[FrameCodec.MaximumLineBytes];
        private int incomingStart;
        private int incomingEnd;
        private int lineLength;
        public int Descriptor => transport.Descriptor;

        public AuthenticatedChannel(ITransport transport, Health health, byte[] token)
        {
            this.transport = transport;
            this.health = health;
            this.token = token;
        }

        private bool TryLine(out byte[] result)
        {
            result = Array.Empty<byte>();
            // A fragmented or flooding peer gets at most one bounded frame per call.
            while (true)
            {
                health.Check();
                if (incomingStart == incomingEnd)
                {
                    int count = transport.Read(incoming, 0, incoming.Length);
                    health.Check();
                    if (count < 0) return false;
                    if (count == 0) throw new EndOfStreamException("host closed input session");
                    incomingStart = 0;
                    incomingEnd = count;
                }
                while (incomingStart < incomingEnd)
                {
                    if (lineLength == line.Length)
                        throw new InvalidDataException("frame exceeds size limit");
                    byte value = incoming[incomingStart++];
                    line[lineLength++] = value;
                    if (value == 10)
                    {
                        result = new byte[lineLength];
                        Buffer.BlockCopy(line, 0, result, 0, lineLength);
                        lineLength = 0;
                        return true;
                    }
                }
            }
        }

        private byte[] AwaitLine()
        {
            while (true)
            {
                if (TryLine(out byte[] result)) return result;
                transport.Wait(false, health.Remaining(health.Deadline));
            }
        }

        public void Handshake(string identity)
        {
            nonce = FrameCodec.Challenge(AwaitLine());
            Send("IDENTITY\t" + Convert.ToBase64String(Encoding.UTF8.GetBytes(identity)));
            health.Accept(FrameCodec.Verify(token, nonce, AwaitLine()));
        }

        public void PumpHeartbeats()
        {
            for (int count = 0; count < 32; count++)
            {
                if (!TryLine(out byte[] result)) return;
                health.Accept(FrameCodec.Verify(token, nonce, result));
            }
        }

        public void Send(string payload)
        {
            health.Check();
            byte[] bytes = FrameCodec.Encode(token, nonce, payload);
            // Snapshot, not a new timeout for each fragment. A blocked outgoing
            // frame cannot acquire extra time from later successful heartbeats.
            long deadline = health.Deadline;
            int offset = 0;
            while (offset < bytes.Length)
            {
                health.Remaining(deadline);
                int count = transport.Write(bytes, offset, bytes.Length - offset);
                health.Remaining(deadline);
                if (count == 0) throw new IOException("socket write failed");
                if (count < 0) transport.Wait(true, health.Remaining(deadline));
                else offset += count;
            }
        }

        public void Dispose() => transport.Dispose();
    }
}
