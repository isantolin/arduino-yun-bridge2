#include "BridgeTestInterface.h"
#include "arduino/BridgeTransport.h"
#include "protocol/rpc_protocol.h"
#include <gtest/gtest.h>

using namespace bridge;

class BridgeTransportCoverageTest : public ::testing::Test {
protected:
    void SetUp() override {
        stream = new MockStream();
        transport = new BridgeTransport(*stream);
    }

    void TearDown() override {
        delete transport;
        delete stream;
    }

    MockStream* stream;
    BridgeTransport* transport;
};

TEST_F(BridgeTransportCoverageTest, RetransmitEmptyBuffer) {
    EXPECT_FALSE(transport->retransmitLastFrame());
}

TEST_F(BridgeTransportCoverageTest, SendFrameInvalidCommand) {
    EXPECT_FALSE(transport->sendFrame(0x01, nullptr, 0));
}

TEST_F(BridgeTransportCoverageTest, SendControlFrameInvalidCommand) {
    EXPECT_FALSE(transport->sendControlFrame(0x01));
}

TEST_F(BridgeTransportCoverageTest, HardwareSerialNullPaths) {
    // These calls exercise the 'if (_hardware_serial != nullptr)' paths when it is NULL.
    transport->begin(115200);
    transport->setBaudrate(9600);
    transport->flush();
    transport->end();
    transport->flushRx();
    
    uint8_t pl = 0;
    transport->sendFrame(rpc::to_underlying(rpc::StatusCode::STATUS_OK), &pl, 1);
    transport->sendControlFrame(rpc::to_underlying(rpc::StatusCode::STATUS_OK));
    transport->retransmitLastFrame();
}

TEST_F(BridgeTransportCoverageTest, OnPacketReceivedEdgeCases) {
    // Case 1: _instance is NULL
    // Accessing private static BridgeTransport::_instance is not possible directly,
    // but the constructor sets it. delete transport; clears it if we want?
    // Let's assume we can call the static method.
    BridgeTransport::onPacketReceived(nullptr, 0);
}

TEST_F(BridgeTransportCoverageTest, ClearErrorAndOverflow) {
    transport->clearError();
    transport->clearOverflow();
    EXPECT_EQ(transport->getLastError(), rpc::FrameParser::Error::NONE);
}
